package payment

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"go-chatgpt/internal/browserhttp"
	"go-chatgpt/internal/domain"
)

const (
	goPayApprovalMaxAttempts = 20
	goPayApprovalRetryDelay  = time.Second
)

var errGoPayPromotionApprovalRequiresNewCheckout = errors.New(
	"GoPay promotional checkout approval was blocked and requires a new Checkout",
)

// goPayApprovalResult keeps the browser session that produced the last
// approval response available to the provider-redirect phase. The entry
// session belongs to executeAttempt and is never closed here; a session made
// for a rotated route is owned by this result and must be released with Close.
// The shared owner makes Close idempotent even if the result value is copied.
type goPayApprovalResult struct {
	Outcome  checkoutApprovalOutcome
	Sent     bool
	Attempts int

	Session  *chatSession
	Client   *http.Client
	ProxyURL string
	Profile  browserhttp.Profile

	owner *goPayApprovalClientOwner
}

type goPayApprovalClientOwner struct {
	once   sync.Once
	client *http.Client
}

func (owner *goPayApprovalClientOwner) Close() {
	if owner == nil {
		return
	}
	owner.once.Do(func() {
		closeHTTPClient(owner.client)
	})
}

func (result *goPayApprovalResult) Close() {
	if result == nil {
		return
	}
	result.owner.Close()
}

// approveGoPayCheckoutWithProxyRotation mirrors the attachment's narrow replay
// boundary for non-promotional GoPay: only an HTTP 403 or result=blocked moves
// to another entry route and creates a completely fresh fingerprint. A
// promotional Checkout is route-bound by its successful preflight, so its first
// blocked response returns a retryable error instead of changing proxy inside
// the accepted Checkout. When configured, the outer workflow then performs a
// new preflight and creates a completely new Checkout. Every other
// HTTP/business/transport error returns immediately. A completely unconfigured
// direct route has no rotation target, so its first blocked response ends the
// approve phase.
func (executor *Executor) approveGoPayCheckoutWithProxyRotation(
	ctx context.Context,
	input domain.CheckoutInput,
	checkout checkoutSession,
	entrySession *chatSession,
) (goPayApprovalResult, error) {
	result := goPayApprovalResult{}
	if executor == nil {
		return result, errors.New("GoPay approve executor is missing")
	}
	if entrySession == nil || entrySession.client == nil {
		return result, errors.New("GoPay approve entry session is missing")
	}
	result.setSession(entrySession, false)

	promotionRouteBound := input.PaymentMethod == domain.PaymentMethodGoPay &&
		input.UsePromo
	routes := goPayApprovalProxyRoutes(entrySession.proxyURL, input.EntryProxies)
	maxAttempts := len(routes)
	if promotionRouteBound {
		maxAttempts = 1
	}
	responseDiagnostics := ""
	for routeIndex := 0; routeIndex < maxAttempts; routeIndex++ {
		proxyURL := routes[routeIndex]
		if routeIndex > 0 {
			wait := executor.retryWait
			if wait == nil {
				wait = waitContext
			}
			if err := wait(ctx, goPayApprovalRetryDelay); err != nil {
				return result, goPayApprovalWithDiagnostics(err, responseDiagnostics)
			}
			rotated, err := executor.newGoPayApprovalRotationSession(
				result.Session,
				proxyURL,
			)
			if err != nil {
				return result, goPayApprovalWithDiagnostics(
					fmt.Errorf("prepare GoPay approve route %d: %w", routeIndex+1, err),
					responseDiagnostics,
				)
			}
			previousOwner := result.owner
			result.setSession(rotated, true)
			previousOwner.Close()
		}

		result.Sent = true
		result.Attempts++
		outcome, err := executor.approveGoPayCheckoutAttempt(
			ctx,
			result.Session,
			checkout,
		)
		if err != nil {
			if strings.TrimSpace(outcome.ResponseDiagnostic) != "" {
				responseDiagnostics = appendUpstreamResponseDiagnostic(
					responseDiagnostics,
					outcome.ResponseDiagnostic,
				)
			} else {
				responseDiagnostics = appendGoPayApprovalErrorDiagnostics(
					responseDiagnostics,
					err,
				)
			}
			outcome.ResponseDiagnostic = responseDiagnostics
			result.Outcome = outcome
			upstream, upstreamFound := paymentErrorAs[*upstreamHTTPError](err)
			if result.Outcome.StatusCode == 0 && upstreamFound {
				result.Outcome.StatusCode = upstream.StatusCode
			}
			switch {
			case result.Outcome.StatusCode == http.StatusUnauthorized:
				return result, goPayApprovalWithDiagnostics(
					permanentWrap("GoPay checkout approval unauthorized", err),
					responseDiagnostics,
				)
			case result.Outcome.StatusCode == http.StatusForbidden ||
				result.Outcome.Result == "blocked":
				result.Outcome.Result = "blocked"
				result.Outcome.Pending = false
				if routeIndex+1 == maxAttempts {
					if promotionRouteBound {
						return result, goPayPromotionApprovalBlockedError(responseDiagnostics)
					}
					return result, goPayApprovalBlockedError(responseDiagnostics)
				}
				continue
			case strings.TrimSpace(result.Outcome.Redirect) != "" ||
				result.Outcome.Result == "approved" ||
				result.Outcome.Result == "requires_action":
				return result, nil
			default:
				return result, goPayApprovalWithDiagnostics(err, responseDiagnostics)
			}
		}

		responseDiagnostics = appendUpstreamResponseDiagnostic(
			responseDiagnostics,
			outcome.ResponseDiagnostic,
		)
		outcome.ResponseDiagnostic = responseDiagnostics
		result.Outcome = outcome
		if outcome.Result == "blocked" {
			result.Outcome.Pending = false
			if routeIndex+1 == maxAttempts {
				if promotionRouteBound {
					return result, goPayPromotionApprovalBlockedError(responseDiagnostics)
				}
				return result, goPayApprovalBlockedError(responseDiagnostics)
			}
			continue
		}
		if strings.TrimSpace(outcome.Redirect) != "" ||
			outcome.Result == "approved" ||
			outcome.Result == "requires_action" {
			return result, nil
		}

		failure := fmt.Errorf(
			"GoPay checkout approval returned unexpected result %q",
			fallbackLabel(outcome.Result, "<missing>"),
		)
		return result, goPayApprovalWithDiagnostics(failure, responseDiagnostics)
	}

	return result, errors.New("GoPay checkout approval did not run")
}

// approveGoPayCheckoutAttempt deliberately decodes the complete response body
// before applying HTTP status policy. The attachment treats a top-level
// result=blocked as a route-rotation signal even on statuses such as 429/500;
// the shared ChatGPT decoder returns on non-2xx before exposing that payload.
func (executor *Executor) approveGoPayCheckoutAttempt(
	ctx context.Context,
	session *chatSession,
	checkout checkoutSession,
) (checkoutApprovalOutcome, error) {
	const (
		path      = "/backend-api/payments/checkout/approve"
		operation = "ChatGPT approve"
	)
	outcome := checkoutApprovalOutcome{}
	if executor == nil {
		return outcome, errors.New("GoPay approve executor is missing")
	}
	if session == nil || session.client == nil {
		return outcome, errors.New("GoPay approve session is missing")
	}

	payload := map[string]any{
		"checkout_session_id": checkout.ID,
		"processor_entity":    checkout.ProcessorEntity,
	}
	body, err := marshalChatGPTPaymentPayload(path, payload)
	if err != nil {
		return outcome, fmt.Errorf("encode GoPay approve request: %w", err)
	}
	referer := fmt.Sprintf(
		"%s/checkout/%s/%s",
		executor.config.ChatGPTBaseURL,
		checkout.ProcessorEntity,
		checkout.ID,
	)
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		executor.config.ChatGPTBaseURL+path,
		bytes.NewReader(body),
	)
	if err != nil {
		return outcome, fmt.Errorf("create GoPay approve request: %w", err)
	}
	request.Header = goPayApprovalHeaders(session, path, referer)
	request = browserhttp.WithExplicitProfileHeaders(request)

	response, err := session.client.Do(request)
	if err != nil {
		requestErr := contextRequestError(ctx, operation, err)
		return outcome, readHTTPResponseError(requestErr, response, operation)
	}
	if response == nil {
		return outcome, errors.New("ChatGPT approve returned an empty response")
	}
	session.syncDeviceIDFromCookies()
	outcome.StatusCode = response.StatusCode
	if response.Body == nil {
		diagnostic := httpResponseDiagnostic(response, operation, nil)
		outcome.ResponseDiagnostic = diagnostic
		if response.StatusCode < http.StatusOK ||
			response.StatusCode >= http.StatusMultipleChoices {
			return outcome, goPayApprovalHTTPStatusError(response, nil, diagnostic)
		}
		return outcome, withUpstreamResponse(
			errors.New("ChatGPT approve returned an empty response body"),
			diagnostic,
		)
	}

	responseBody, readErr := io.ReadAll(response.Body)
	if closeErr := response.Body.Close(); closeErr != nil {
		readErr = errors.Join(
			readErr,
			fmt.Errorf("close %s response: %w", operation, closeErr),
		)
	}
	diagnostic := httpResponseDiagnostic(response, operation, responseBody)
	outcome.ResponseDiagnostic = diagnostic
	if readErr != nil {
		return outcome, withUpstreamResponse(
			&upstreamResponseReadError{
				Operation:  operation,
				StatusCode: response.StatusCode,
				Err:        readErr,
			},
			diagnostic,
		)
	}

	var decoded map[string]any
	decodeErr := json.Unmarshal(responseBody, &decoded)
	if decodeErr == nil && decoded == nil {
		decoded = map[string]any{}
	}
	if decodeErr == nil {
		if result, ok := decoded["result"].(string); ok {
			outcome.Result = strings.ToLower(strings.TrimSpace(result))
		}
		outcome.Redirect = checkoutApprovalRedirect(decoded)
	}

	if response.StatusCode < http.StatusOK ||
		response.StatusCode >= http.StatusMultipleChoices {
		return outcome, goPayApprovalHTTPStatusError(
			response,
			responseBody,
			diagnostic,
		)
	}
	if decodeErr != nil {
		return outcome, withUpstreamResponse(
			fmt.Errorf("%s: decode upstream JSON: %w", operation, decodeErr),
			diagnostic,
		)
	}
	return outcome, nil
}

func goPayApprovalHeaders(
	session *chatSession,
	path string,
	referer string,
) http.Header {
	headers := make(http.Header)
	browserhttp.ApplyLowEntropyHeaders(headers, session.profile)
	headers.Set("Accept-Encoding", "gzip")
	headers.Set("Authorization", "Bearer "+session.accessToken)
	headers.Set("Content-Type", "application/json")
	headers.Set("Accept", "*/*")
	headers.Set("Accept-Language", "id-ID,id;q=0.9,en;q=0.8")
	headers.Set("Origin", session.executor.config.ChatGPTBaseURL)
	headers.Set("Referer", referer)
	headers.Set("oai-device-id", session.deviceID)
	headers.Set("oai-language", "id-ID")
	headers.Set("x-openai-target-path", path)
	headers.Set("x-openai-target-route", path)
	return headers
}

// goPayApprovalHTTPStatusError delegates classification to the common decoder
// after the GoPay-specific caller has already inspected the top-level payload.
// Replaying an in-memory body preserves the same complete headers/body
// diagnostic and provider-error classifications used by the rest of payment.
func goPayApprovalHTTPStatusError(
	response *http.Response,
	body []byte,
	diagnostic string,
) error {
	if response == nil {
		return errors.New("ChatGPT approve returned an empty response")
	}
	replayed := new(http.Response)
	*replayed = *response
	replayed.Body = io.NopCloser(bytes.NewReader(body))
	_, err := decodeJSONResponseDetails(replayed, "ChatGPT approve")
	if err != nil {
		return err
	}
	return withUpstreamResponse(
		fmt.Errorf("ChatGPT approve: upstream returned HTTP %d", response.StatusCode),
		diagnostic,
	)
}

func (result *goPayApprovalResult) setSession(session *chatSession, owned bool) {
	result.Session = session
	result.Client = nil
	result.ProxyURL = ""
	result.Profile = browserhttp.Profile{}
	result.owner = nil
	if session == nil {
		return
	}
	result.Client = session.client
	result.ProxyURL = strings.TrimSpace(session.proxyURL)
	result.Profile = session.profile
	if owned {
		result.owner = &goPayApprovalClientOwner{client: session.client}
	}
}

// goPayApprovalProxyRoutes builds the attachment's independent 20-attempt
// approve schedule. The current Checkout route is always used first. Pool
// entries are reusable; when more than one distinct route exists, the schedule
// advances circularly so consecutive attempts never use the same route.
func goPayApprovalProxyRoutes(current string, candidates []string) []string {
	current = strings.TrimSpace(current)
	pool := make([]string, 0, len(candidates)+1)
	for _, candidate := range candidates {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" || goPayApprovalRoutesContain(pool, candidate) {
			continue
		}
		pool = append(pool, candidate)
	}
	if current != "" && !goPayApprovalRoutesContain(pool, current) {
		pool = append([]string{current}, pool...)
	}

	routes := make([]string, 0, goPayApprovalMaxAttempts)
	routes = append(routes, current)
	if len(pool) == 0 {
		return routes
	}
	if len(pool) == 1 {
		for len(routes) < goPayApprovalMaxAttempts {
			routes = append(routes, pool[0])
		}
		return routes
	}

	nextIndex := 0
	for index, candidate := range pool {
		if sameRoute(candidate, current) {
			nextIndex = (index + 1) % len(pool)
			break
		}
	}
	for len(routes) < goPayApprovalMaxAttempts {
		candidate := pool[nextIndex]
		nextIndex = (nextIndex + 1) % len(pool)
		if sameRoute(candidate, routes[len(routes)-1]) {
			continue
		}
		routes = append(routes, candidate)
	}
	return routes
}

func goPayApprovalRoutesContain(routes []string, candidate string) bool {
	for _, route := range routes {
		if sameRoute(route, candidate) {
			return true
		}
	}
	return false
}

func (executor *Executor) newGoPayApprovalRotationSession(
	source *chatSession,
	proxyURL string,
) (*chatSession, error) {
	if source == nil || source.client == nil {
		return nil, errors.New("source GoPay approve session is missing")
	}

	source.mu.Lock()
	accessToken := source.accessToken
	accountID := source.accountID
	previous := paymentFingerprint{
		profile:           source.profile,
		deviceID:          source.deviceID,
		locale:            source.locale,
		acceptLanguage:    source.acceptLanguage,
		timeZone:          source.timeZone,
		timeZonePersisted: source.timeZonePersisted,
	}
	source.mu.Unlock()

	if strings.TrimSpace(previous.profile.Name) == "" {
		return nil, errors.New("source GoPay browser profile is missing")
	}
	if strings.TrimSpace(previous.deviceID) == "" {
		return nil, errors.New("source GoPay device identifier is missing")
	}
	fingerprint, err := executor.newGoPayRetryFingerprint(previous)
	if err != nil {
		return nil, fmt.Errorf("prepare fresh GoPay approve retry fingerprint: %w", err)
	}
	rotated, err := executor.newChatSessionWithFingerprint(
		accessToken,
		proxyURL,
		fingerprint,
	)
	if err != nil {
		return nil, err
	}
	rotated.configureCheckoutMarket(
		string(domain.PaymentMethodGoPay),
		goPayCountry,
		goPayCurrency,
	)
	rotated.mu.Lock()
	rotated.accountID = accountID
	rotated.mu.Unlock()
	return rotated, nil
}

func appendGoPayApprovalErrorDiagnostics(existing string, err error) string {
	for _, node := range snapshotUpstreamResponseErrors(err).nodes {
		existing = appendUpstreamResponseDiagnostic(
			existing,
			safeUpstreamResponseDiagnostic(node.err),
		)
	}
	return existing
}

func goPayApprovalWithDiagnostics(err error, diagnostics string) error {
	if err == nil {
		return nil
	}
	return withUpstreamResponse(err, diagnostics)
}

func goPayApprovalBlockedError(diagnostics string) error {
	return goPayApprovalWithDiagnostics(
		permanentWrap("GoPay checkout approval", errApproveBlocked),
		diagnostics,
	)
}

func goPayPromotionApprovalBlockedError(diagnostics string) error {
	return goPayApprovalWithDiagnostics(
		errGoPayPromotionApprovalRequiresNewCheckout,
		diagnostics,
	)
}
