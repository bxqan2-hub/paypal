package payment

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"go-chatgpt/internal/browserhttp"
)

const (
	goPayAttachmentStripeRuntimeVersion = "c00af4ce81"

	goPayAttachmentPollDefaultMaxAttempts = 60
	goPayAttachmentPollDefaultInterval    = 2 * time.Second
	goPayAttachmentProviderDefaultMaxHops = 6
	goPayAttachmentProviderMaximumHops    = 12
)

var goPayAttachmentProviderHostSuffixes = []string{
	"gopay.co.id",
	"gopay.id",
	"gojek.com",
	"goto.com",
	"midtrans.com",
}

var goPayAttachmentMidtransHTMLURLPattern = regexp.MustCompile(
	`(?i)https://app\.midtrans\.com/snap/v4/redirection/[0-9a-z-]+(?:\?[^"'<> \t\r\n]*)?(?:#[^"'<> \t\r\n]*)?`,
)

var goPayAttachmentStripeHeaderOrder = []string{
	"sec-ch-ua",
	"sec-ch-ua-mobile",
	"sec-ch-ua-platform",
	"user-agent",
	"authorization",
	"accept",
	"sec-fetch-site",
	"sec-fetch-mode",
	"sec-fetch-dest",
	"accept-encoding",
	"accept-language",
	"content-type",
	"origin",
	"referer",
}

var goPayAttachmentActivationHeaderOrder = []string{
	"sec-ch-ua",
	"sec-ch-ua-mobile",
	"sec-ch-ua-platform",
	"user-agent",
	"accept",
	"accept-encoding",
	"accept-language",
	"referer",
}

var goPayAttachmentPreConfirmFieldOrder = []string{
	"eid",
	"payment_method_type",
	"key",
	"_stripe_version",
}

var goPayAttachmentPaymentMethodFieldOrder = []string{
	"type",
	"billing_details[name]",
	"billing_details[email]",
	"billing_details[phone]",
	"billing_details[address][country]",
	"billing_details[address][line1]",
	"billing_details[address][line2]",
	"billing_details[address][city]",
	"billing_details[address][state]",
	"billing_details[address][postal_code]",
	"guid",
	"muid",
	"sid",
	"key",
	"_stripe_version",
	"payment_user_agent",
	"client_attribution_metadata[client_session_id]",
	"client_attribution_metadata[checkout_session_id]",
	"client_attribution_metadata[merchant_integration_source]",
	"client_attribution_metadata[merchant_integration_version]",
	"client_attribution_metadata[payment_method_selection_flow]",
	"client_attribution_metadata[checkout_config_id]",
}

var goPayAttachmentConfirmFieldOrder = []string{
	"elements_session_client[client_betas][0]",
	"elements_session_client[client_betas][1]",
	"elements_session_client[elements_init_source]",
	"elements_session_client[referrer_host]",
	"elements_session_client[locale]",
	"elements_session_client[is_aggregation_expected]",
	"elements_options_client[saved_payment_method][enable_save]",
	"elements_options_client[saved_payment_method][enable_redisplay]",
	"elements_session_client[stripe_js_id]",
	"elements_session_client[session_id]",
	"eid",
	"payment_method",
	"expected_amount",
	"tax_id_collection[purchasing_as_business]",
	"expected_payment_method_type",
	"return_url",
	"_stripe_version",
	"guid",
	"muid",
	"sid",
	"key",
	"version",
	"init_checksum",
	"client_attribution_metadata[client_session_id]",
	"client_attribution_metadata[checkout_session_id]",
	"client_attribution_metadata[merchant_integration_source]",
	"client_attribution_metadata[merchant_integration_version]",
	"client_attribution_metadata[payment_method_selection_flow]",
	"link_brand",
	"client_attribution_metadata[checkout_config_id]",
}

type goPayAttachmentBrowserIDs struct {
	ClientSessionID string
	GUID            string
	MUID            string
	SID             string
}

type goPayAttachmentPaymentMethod struct {
	ID       string
	Response protocolJSONResponse
}

type goPayAttachmentConfirmation struct {
	Response protocolJSONResponse
	Snapshot stripeSnapshot
	Redirect string
}

type goPayAttachmentPollPolicy struct {
	MaxAttempts int
	Interval    time.Duration
}

type goPayAttachmentPollResult struct {
	Response protocolJSONResponse
	Redirect string
	Attempts int
}

type goPayAttachmentProviderRedirectPolicy struct {
	MaxHops             int
	AllowedHostSuffixes []string
}

// activateAttachmentCSGoPayCheckout reproduces the attachment's best-effort
// Checkout page activation. Neither navigation is authoritative: failures and
// non-success statuses are recorded for protocol diagnostics, then the caller
// continues to Stripe init with the canonical checkout.stripe.com page.
func (executor *Executor) activateAttachmentCSGoPayCheckout(
	ctx context.Context,
	client *http.Client,
	profile browserhttp.Profile,
	checkout checkoutSession,
) string {
	checkoutPage := attachmentCSGoPayCheckoutPageURL(checkout.ID)
	if executor == nil || client == nil || !validGoPayCheckoutSessionID(strings.TrimSpace(checkout.ID)) {
		return checkoutPage
	}
	resolvedProfile, err := attachmentCSGoPayProfile(profile)
	if err != nil {
		reportProtocolWarning(ctx, 23, "stripe", "GoPay Checkout 页面预热跳过: %s", err)
		return checkoutPage
	}
	referer := strings.TrimRight(executor.config.ChatGPTBaseURL, "/") + "/"
	for _, target := range []string{
		"https://pay.openai.com/c/pay/" + url.PathEscape(strings.TrimSpace(checkout.ID)),
		checkoutPage,
	} {
		request, requestErr := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
		if requestErr != nil {
			reportProtocolWarning(ctx, 23, "stripe", "GoPay Checkout 页面预热请求创建失败: %s", requestErr)
			continue
		}
		browserhttp.ApplyLowEntropyHeaders(request.Header, resolvedProfile)
		request.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
		request.Header.Set("Accept-Encoding", "gzip")
		request.Header.Set("Accept-Language", "id-ID,id;q=0.9,en;q=0.8")
		request.Header.Set("Referer", referer)
		request = browserhttp.WithHeaderOrder(request, goPayAttachmentActivationHeaderOrder...)

		response, requestErr := doHTTPPreservingErrorResponse(client, request)
		if requestErr != nil {
			requestErr = contextRequestError(ctx, "Stripe GoPay Checkout activation", requestErr)
			requestErr = readHTTPResponseError(
				requestErr,
				response,
				"Stripe GoPay Checkout activation",
			)
			reportProtocolWarning(
				ctx,
				23,
				"stripe",
				"GoPay Checkout 页面预热失败: %s",
				paymentFailureLogDiagnostic(requestErr),
			)
			continue
		}
		if response == nil {
			reportProtocolWarning(ctx, 23, "stripe", "GoPay Checkout 页面预热返回空响应")
			continue
		}
		body, readErr := readAttachmentCSGoPayResponseBody(response)
		if readErr != nil {
			diagnostic := httpResponseDiagnostic(
				response,
				"Stripe GoPay Checkout activation",
				body,
			)
			reportProtocolWarning(
				ctx,
				23,
				"stripe",
				"GoPay Checkout 页面预热响应读取失败: %s",
				diagnostic,
			)
			continue
		}
		if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
			reportProtocolWarning(
				ctx,
				23,
				"stripe",
				"GoPay Checkout 页面预热返回非成功状态，继续初始化: %s",
				httpResponseDiagnostic(
					response,
					"Stripe GoPay Checkout activation",
					body,
				),
			)
			continue
		}
		reportProtocolInfo(
			ctx,
			23,
			"stripe",
			"GoPay Checkout 页面预热完成: host=%s, http=%d",
			request.URL.Hostname(),
			response.StatusCode,
		)
	}
	return checkoutPage
}

// stripeInitAttachmentCSGoPay is the Go reconstruction of the attachment's
// Stripe bootstrap/init boundary. A fresh stripe_js_id is generated for every
// call, while response-owned Elements/config/checksum/amount/currency/link
// context falls back to the preceding init exactly as the attachment does.
func (executor *Executor) stripeInitAttachmentCSGoPay(
	ctx context.Context,
	client *http.Client,
	profile browserhttp.Profile,
	checkout checkoutSession,
	previous stripeSnapshot,
) (stripeSnapshot, error) {
	return executor.stripeInitAttachmentCSGoPayMode(
		ctx,
		client,
		profile,
		checkout,
		previous,
		true,
	)
}

// stripeBootstrapAttachmentCSGoPay performs the attachment's first, context-
// building init. That response may be sparse; availability, currency, and
// pricing become authoritative only after checkout/update and the second init.
func (executor *Executor) stripeBootstrapAttachmentCSGoPay(
	ctx context.Context,
	client *http.Client,
	profile browserhttp.Profile,
	checkout checkoutSession,
	previous stripeSnapshot,
) (stripeSnapshot, error) {
	return executor.stripeInitAttachmentCSGoPayMode(
		ctx,
		client,
		profile,
		checkout,
		previous,
		false,
	)
}

func (executor *Executor) stripeInitAttachmentCSGoPayMode(
	ctx context.Context,
	client *http.Client,
	profile browserhttp.Profile,
	checkout checkoutSession,
	previous stripeSnapshot,
	validatePaymentState bool,
) (stripeSnapshot, error) {
	const operation = "Stripe GoPay attachment init"
	if executor == nil {
		return stripeSnapshot{}, errors.New(operation + ": executor is missing")
	}
	canonical, err := canonicalCSGoPayCheckout(checkout)
	if err != nil {
		return stripeSnapshot{}, err
	}
	if validatePaymentState {
		if bindingErr := validatePartialAttachmentCSGoPayInitBinding(previous, canonical.ID); bindingErr != nil {
			return stripeSnapshot{}, withUpstreamResponse(
				permanentWrap("Stripe GoPay prior init binding", bindingErr),
				previous.ResponseDiagnostic,
			)
		}
	}
	resolvedProfile, err := attachmentCSGoPayProfile(profile)
	if err != nil {
		return stripeSnapshot{}, fmt.Errorf("%s: %w", operation, err)
	}
	stripeJSID, err := executor.newUUID()
	if err != nil {
		return stripeSnapshot{}, fmt.Errorf("%s: create stripe_js_id: %w", operation, err)
	}

	current, err := executor.ensureStripeContextDeviceIDs(previous.Context)
	if err != nil {
		return stripeSnapshot{}, fmt.Errorf("%s: prepare Stripe context: %w", operation, err)
	}
	current.StripeJSID = stripeJSID
	current.BrowserLocale = "id-ID"
	current.ElementsLocale = "en"
	current.SavedPaymentMethodMode = "auto"
	if err := current.setBrowserTimeZone("Asia/Jakarta", current.browserTimeZonePersisted); err != nil {
		return stripeSnapshot{}, fmt.Errorf("%s: prepare browser time zone: %w", operation, err)
	}
	if strings.TrimSpace(current.ElementsSessionID) == "" {
		elementsSuffix, suffixErr := executor.newHexID(11)
		if suffixErr != nil {
			return stripeSnapshot{}, fmt.Errorf("%s: create Elements session fallback: %w", operation, suffixErr)
		}
		current.ElementsSessionID = "elements_session_" + elementsSuffix
	}

	publishableKey := firstNonEmptyAttachmentCSGoPayValue(
		checkout.PublishableKey,
		previous.PublishableKey,
	)
	if publishableKey == "" {
		return stripeSnapshot{}, withUpstreamResponse(
			permanent("Stripe GoPay attachment init: Checkout publishable key is missing"),
			previous.ResponseDiagnostic,
		)
	}
	values := attachmentCSGoPayElementsValues(stripeSnapshot{Context: current})
	// Each attachment init starts a new Elements client bootstrap and therefore
	// deliberately omits the prior elements_session_client[session_id].
	values.Del("elements_session_client[session_id]")
	values.Set("key", publishableKey)
	values.Set("eid", "NA")
	values.Set("browser_locale", "id-ID")
	values.Set("browser_timezone", "Asia/Jakarta")
	values.Set("redirect_type", "url")
	values.Set("_stripe_version", stripeVersion)

	response, err := executor.doAttachmentCSGoPayStripeFormResponse(
		ctx,
		client,
		http.MethodPost,
		"/v1/payment_pages/"+url.PathEscape(canonical.ID)+"/init",
		values,
		nil,
		resolvedProfile,
		canonical,
		publishableKey,
		operation,
	)
	if err != nil {
		return stripeSnapshot{}, err
	}
	observedPayload := response.Payload
	if validatePaymentState {
		if bindingErr := validateAttachmentCSGoPayInitBindingContinuity(
			previous.Payload,
			observedPayload,
		); bindingErr != nil {
			return stripeSnapshot{}, withUpstreamResponse(
				permanentWrap("Stripe GoPay init binding continuity", bindingErr),
				appendUpstreamResponseDiagnostic(
					previous.ResponseDiagnostic,
					response.ResponseDiagnostic,
				),
			)
		}
	}
	response.Payload = mergeAttachmentCSGoPayInitBindings(
		previous.Payload,
		response.Payload,
	)

	baseline := previous
	baseline.Context = current
	baseline.PublishableKey = publishableKey
	if strings.TrimSpace(baseline.HostedURL) == "" {
		baseline.HostedURL = attachmentCSGoPayCheckoutPageURL(canonical.ID)
	}
	currentSnapshot := attachmentCSGoPaySnapshotFromResponse(baseline, response)
	if hostedURL := strings.TrimSpace(mapString(response.Payload, "stripe_hosted_url")); hostedURL != "" {
		currentSnapshot.HostedURL = hostedURL
	}
	linkBrand := explicitAttachmentCSGoPayLinkBrand(response.Payload)
	if linkBrand == "" {
		linkBrand = explicitAttachmentCSGoPayLinkBrand(previous.Payload)
	}
	if linkBrand == "" {
		linkBrand = "link"
	}
	currentSnapshot = withAttachmentCSGoPayLinkBrand(currentSnapshot, linkBrand)
	if validatePaymentState {
		if err := validateAttachmentCSGoPayPaymentState(
			observedPayload,
			currentSnapshot.ResponseDiagnostic,
		); err != nil {
			return stripeSnapshot{}, err
		}
		if err := validateCSGoPayStripeInit(currentSnapshot, canonical.ID); err != nil {
			return stripeSnapshot{}, err
		}
	}
	return currentSnapshot, nil
}

// validatePartialAttachmentCSGoPayInitBinding admits a sparse bootstrap while
// rejecting malformed binding fields that were explicitly returned. The first
// later init that supplies all fields establishes the complete Payment Page
// anchor; validateCSGoPayStripeInit performs that complete check after the
// current response has been merged with these carried values.
func validatePartialAttachmentCSGoPayInitBinding(
	snapshot stripeSnapshot,
	expectedID string,
) error {
	if !validGoPayCheckoutSessionID(strings.TrimSpace(expectedID)) {
		return errors.New("checkout session identity is unavailable")
	}
	for _, field := range []struct {
		name     string
		validate func(string) error
	}{
		{
			name: "id",
			validate: func(value string) error {
				if !validCheckoutSessionIDWithPrefix(value, "ppage_") {
					return errors.New("top-level payment page id is missing or invalid")
				}
				return nil
			},
		},
		{
			name: "session_id",
			validate: func(value string) error {
				if value != expectedID {
					return fmt.Errorf(
						"checkout session changed: expected=%s actual=%s",
						expectedID,
						fallbackLabel(value, "<missing>"),
					)
				}
				return nil
			},
		},
		{name: "config_id"},
		{name: "init_checksum"},
	} {
		if _, exists := snapshot.Payload[field.name]; !exists {
			continue
		}
		value, err := goPayRequiredTopLevelString(snapshot.Payload, field.name)
		if err != nil {
			return err
		}
		if field.validate != nil {
			if err := field.validate(value); err != nil {
				return err
			}
		}
		switch field.name {
		case "config_id":
			if contextValue := strings.TrimSpace(snapshot.Context.ConfigID); contextValue != "" &&
				contextValue != value {
				return errors.New("top-level config_id disagrees with Stripe context")
			}
		case "init_checksum":
			if contextValue := strings.TrimSpace(snapshot.Context.InitChecksum); contextValue != "" &&
				contextValue != value {
				return errors.New("top-level init_checksum disagrees with Stripe context")
			}
		}
	}
	return nil
}

// validateAttachmentCSGoPayInitBindingContinuity prevents a later init from
// replacing the Payment Page or Checkout identity. Stripe may explicitly
// refresh config_id and init_checksum on a later init; those current-first
// values are applied by attachmentCSGoPaySnapshotFromResponse and validated
// against the resulting Stripe context.
func validateAttachmentCSGoPayInitBindingContinuity(previous, current map[string]any) error {
	for _, key := range []string{"id", "session_id"} {
		previousValue, previousExists := previous[key]
		currentValue, currentExists := current[key]
		if !previousExists || !currentExists {
			continue
		}
		before := strings.TrimSpace(scalarString(previousValue))
		after := strings.TrimSpace(scalarString(currentValue))
		if before != after {
			return fmt.Errorf(
				"top-level %s changed across init: expected=%s actual=%s",
				key,
				fallbackLabel(before, "<missing>"),
				fallbackLabel(after, "<missing>"),
			)
		}
	}
	return nil
}

func validateAttachmentCSGoPayPaymentState(
	payload map[string]any,
	diagnostic string,
) error {
	fail := func(message string) error {
		return withUpstreamResponse(
			permanent("Stripe GoPay payment state: "+message),
			diagnostic,
		)
	}
	currency := strings.ToLower(strings.TrimSpace(mapString(payload, "currency")))
	if currency != strings.ToLower(goPayCurrency) {
		return fail(fmt.Sprintf(
			"checkout currency mismatch: got=%s want=idr",
			fallbackLabel(currency, "<missing>"),
		))
	}
	methods := availablePaymentMethods(payload)
	if !containsMethod(methods, "gopay") {
		return fail(fmt.Sprintf(
			"gopay is unavailable (available=%s)",
			stripeMethodsSummary(methods),
		))
	}
	return nil
}

// mergeAttachmentCSGoPayInitBindings carries only the structural Payment Page
// anchor across attachment-style init responses. Amount, currency, payment
// methods, and pricing remain response-owned and are validated from the current
// response. A present binding field is never overwritten, so malformed or
// conflicting session/config data still reaches the strict validator.
func mergeAttachmentCSGoPayInitBindings(
	previous,
	current map[string]any,
) map[string]any {
	merged := make(map[string]any, len(current)+4)
	for key, value := range current {
		merged[key] = value
	}
	for _, key := range []string{"id", "session_id", "config_id", "init_checksum"} {
		if _, exists := merged[key]; exists {
			continue
		}
		if value, exists := previous[key]; exists {
			merged[key] = value
		}
	}
	return merged
}

// newAttachmentCSGoPayBrowserIDs reproduces the identifiers created directly
// before the attachment implementation creates its pm_ object. In particular,
// guid/muid/sid are UUID-shaped values followed by eight hexadecimal bytes;
// they are intentionally different from the 32-character IDs used by the
// generic Stripe flow and the 42-character IDs used by Hosted Checkout.
func (executor *Executor) newAttachmentCSGoPayBrowserIDs() (goPayAttachmentBrowserIDs, error) {
	clientSessionID, err := executor.newUUID()
	if err != nil {
		return goPayAttachmentBrowserIDs{}, fmt.Errorf("create GoPay client session ID: %w", err)
	}
	newBrowserID := func(label string) (string, error) {
		base, err := executor.newUUID()
		if err != nil {
			return "", fmt.Errorf("create GoPay %s UUID: %w", label, err)
		}
		suffix, err := executor.newHexID(8)
		if err != nil {
			return "", fmt.Errorf("create GoPay %s suffix: %w", label, err)
		}
		return base + suffix, nil
	}
	guid, err := newBrowserID("guid")
	if err != nil {
		return goPayAttachmentBrowserIDs{}, err
	}
	muid, err := newBrowserID("muid")
	if err != nil {
		return goPayAttachmentBrowserIDs{}, err
	}
	sid, err := newBrowserID("sid")
	if err != nil {
		return goPayAttachmentBrowserIDs{}, err
	}
	return goPayAttachmentBrowserIDs{
		ClientSessionID: clientSessionID,
		GUID:            guid,
		MUID:            muid,
		SID:             sid,
	}, nil
}

func (executor *Executor) preConfirmAttachmentCSGoPay(
	ctx context.Context,
	client *http.Client,
	profile browserhttp.Profile,
	checkout checkoutSession,
	snapshot stripeSnapshot,
) (protocolJSONResponse, error) {
	const operation = "Stripe GoPay pre_confirm"
	if executor == nil {
		return protocolJSONResponse{}, errors.New(operation + ": executor is missing")
	}
	if client == nil {
		return protocolJSONResponse{}, errors.New(operation + ": HTTP client is missing")
	}
	if !validGoPayCheckoutSessionID(strings.TrimSpace(checkout.ID)) ||
		strings.TrimSpace(snapshot.PublishableKey) == "" {
		return protocolJSONResponse{}, withUpstreamResponse(
			permanent(operation+": Stripe checkout context is incomplete"),
			snapshot.ResponseDiagnostic,
		)
	}
	eid, err := executor.newUUID()
	if err != nil {
		return protocolJSONResponse{}, fmt.Errorf("%s: create eid: %w", operation, err)
	}
	values := url.Values{
		"eid":                 {eid},
		"payment_method_type": {"gopay"},
		"key":                 {snapshot.PublishableKey},
		"_stripe_version":     {stripeVersion},
	}
	return executor.doAttachmentCSGoPayStripeFormResponse(
		ctx,
		client,
		http.MethodPost,
		"/v1/payment_pages/"+url.PathEscape(checkout.ID)+"/pre_confirm",
		values,
		goPayAttachmentPreConfirmFieldOrder,
		profile,
		checkout,
		snapshot.PublishableKey,
		operation,
	)
}

func (executor *Executor) createAttachmentCSGoPayPaymentMethod(
	ctx context.Context,
	client *http.Client,
	profile browserhttp.Profile,
	checkout checkoutSession,
	snapshot stripeSnapshot,
	ids goPayAttachmentBrowserIDs,
	billing billingDetails,
) (goPayAttachmentPaymentMethod, error) {
	const operation = "Stripe GoPay payment method"
	if executor == nil {
		return goPayAttachmentPaymentMethod{}, errors.New(operation + ": executor is missing")
	}
	if client == nil {
		return goPayAttachmentPaymentMethod{}, errors.New(operation + ": HTTP client is missing")
	}
	if !validGoPayCheckoutSessionID(strings.TrimSpace(checkout.ID)) ||
		strings.TrimSpace(snapshot.PublishableKey) == "" {
		return goPayAttachmentPaymentMethod{}, withUpstreamResponse(
			permanent(operation+": Stripe checkout context is incomplete"),
			snapshot.ResponseDiagnostic,
		)
	}
	if err := validateAttachmentCSGoPayBrowserIDs(ids); err != nil {
		return goPayAttachmentPaymentMethod{}, fmt.Errorf("%s: %w", operation, err)
	}
	values := url.Values{
		"type":                                  {"gopay"},
		"billing_details[name]":                 {billing.Name},
		"billing_details[email]":                {billing.Email},
		"billing_details[phone]":                {billing.Phone},
		"billing_details[address][country]":     {billing.Country},
		"billing_details[address][line1]":       {billing.Line1},
		"billing_details[address][line2]":       {billing.Line2},
		"billing_details[address][city]":        {billing.City},
		"billing_details[address][state]":       {billing.State},
		"billing_details[address][postal_code]": {billing.PostalCode},
		"guid":                                  {ids.GUID},
		"muid":                                  {ids.MUID},
		"sid":                                   {ids.SID},
		"key":                                   {snapshot.PublishableKey},
		"_stripe_version":                       {stripeVersion},
		"payment_user_agent": {"stripe.js/" + goPayAttachmentStripeRuntimeVersion +
			"; stripe-js-v3/" + goPayAttachmentStripeRuntimeVersion + "; checkout"},
		"client_attribution_metadata[client_session_id]":             {ids.ClientSessionID},
		"client_attribution_metadata[checkout_session_id]":           {checkout.ID},
		"client_attribution_metadata[merchant_integration_source]":   {"checkout"},
		"client_attribution_metadata[merchant_integration_version]":  {"custom_checkout"},
		"client_attribution_metadata[payment_method_selection_flow]": {"merchant_specified"},
	}
	if configID := strings.TrimSpace(snapshot.Context.ConfigID); configID != "" {
		values.Set("client_attribution_metadata[checkout_config_id]", configID)
	}
	response, err := executor.doAttachmentCSGoPayStripeFormResponse(
		ctx,
		client,
		http.MethodPost,
		"/v1/payment_methods",
		values,
		goPayAttachmentPaymentMethodFieldOrder,
		profile,
		checkout,
		snapshot.PublishableKey,
		operation,
	)
	if err != nil {
		return goPayAttachmentPaymentMethod{}, err
	}
	paymentMethodID := strings.TrimSpace(mapString(response.Payload, "id"))
	if !strings.HasPrefix(paymentMethodID, "pm_") ||
		strings.ContainsAny(paymentMethodID, "\x00\r\n") {
		return goPayAttachmentPaymentMethod{}, withUpstreamResponse(
			errors.New(operation+": response is missing a pm_ identifier"),
			response.ResponseDiagnostic,
		)
	}
	return goPayAttachmentPaymentMethod{
		ID:       paymentMethodID,
		Response: response,
	}, nil
}

func (executor *Executor) confirmAttachmentCSGoPayWithPaymentMethod(
	ctx context.Context,
	client *http.Client,
	profile browserhttp.Profile,
	checkout checkoutSession,
	snapshot stripeSnapshot,
	ids goPayAttachmentBrowserIDs,
	paymentMethodID string,
) (goPayAttachmentConfirmation, error) {
	const operation = "Stripe GoPay pm_ confirm"
	if executor == nil {
		return goPayAttachmentConfirmation{}, errors.New(operation + ": executor is missing")
	}
	if client == nil {
		return goPayAttachmentConfirmation{}, errors.New(operation + ": HTTP client is missing")
	}
	if !validGoPayCheckoutSessionID(strings.TrimSpace(checkout.ID)) ||
		strings.TrimSpace(snapshot.PublishableKey) == "" {
		return goPayAttachmentConfirmation{}, withUpstreamResponse(
			permanent(operation+": Stripe checkout context is incomplete"),
			snapshot.ResponseDiagnostic,
		)
	}
	if err := validateAttachmentCSGoPayBrowserIDs(ids); err != nil {
		return goPayAttachmentConfirmation{}, fmt.Errorf("%s: %w", operation, err)
	}
	paymentMethodID = strings.TrimSpace(paymentMethodID)
	if !strings.HasPrefix(paymentMethodID, "pm_") ||
		strings.ContainsAny(paymentMethodID, "\x00\r\n") {
		return goPayAttachmentConfirmation{}, errors.New(operation + ": payment method is invalid")
	}

	values := attachmentCSGoPayElementsValues(snapshot)
	values.Set("eid", "NA")
	values.Set("payment_method", paymentMethodID)
	values.Set("expected_amount", attachmentCSGoPayExpectedAmount(snapshot))
	values.Set("tax_id_collection[purchasing_as_business]", "false")
	values.Set("expected_payment_method_type", "gopay")
	values.Set("return_url", executor.attachmentCSGoPayConfirmReturnURL(checkout))
	values.Set("_stripe_version", stripeVersion)
	values.Set("guid", ids.GUID)
	values.Set("muid", ids.MUID)
	values.Set("sid", ids.SID)
	values.Set("key", snapshot.PublishableKey)
	values.Set("version", goPayAttachmentStripeRuntimeVersion)
	values.Set("init_checksum", firstNonEmptyAttachmentCSGoPayValue(
		mapString(snapshot.Payload, "init_checksum"),
		snapshot.Context.InitChecksum,
	))
	values.Set("client_attribution_metadata[client_session_id]", ids.ClientSessionID)
	values.Set("client_attribution_metadata[checkout_session_id]", checkout.ID)
	values.Set("client_attribution_metadata[merchant_integration_source]", "checkout")
	values.Set("client_attribution_metadata[merchant_integration_version]", "custom_checkout")
	values.Set("client_attribution_metadata[payment_method_selection_flow]", "merchant_specified")
	values.Set("link_brand", attachmentCSGoPayLinkBrand(snapshot.Payload))
	if configID := strings.TrimSpace(snapshot.Context.ConfigID); configID != "" {
		values.Set("client_attribution_metadata[checkout_config_id]", configID)
	}

	response, err := executor.doAttachmentCSGoPayStripeFormResponse(
		ctx,
		client,
		http.MethodPost,
		"/v1/payment_pages/"+url.PathEscape(checkout.ID)+"/confirm",
		values,
		goPayAttachmentConfirmFieldOrder,
		profile,
		checkout,
		snapshot.PublishableKey,
		operation,
	)
	if err != nil {
		return goPayAttachmentConfirmation{}, err
	}
	confirmed := attachmentCSGoPaySnapshotFromResponse(snapshot, response)
	return goPayAttachmentConfirmation{
		Response: response,
		Snapshot: confirmed,
		Redirect: extractAttachmentCSGoPayRedirectURL(response.Payload),
	}, nil
}

// extractAttachmentCSGoPayRedirectURL mirrors the attachment's Stripe
// confirm/poll redirect boundary. It scans every object, prefers next_action
// candidates globally, and accepts only the two explicit fallback fields.
// The wider return_url/url/next_url set belongs to ChatGPT approve responses,
// which are parsed separately by checkoutApprovalRedirect.
func extractAttachmentCSGoPayRedirectURL(value any) string {
	nextActionCandidates := make([]string, 0, 4)
	fallbackCandidates := make([]string, 0, 4)
	var walk func(any, int)
	walk = func(current any, depth int) {
		if depth > 64 {
			return
		}
		switch typed := current.(type) {
		case map[string]any:
			if nextAction, ok := typed["next_action"].(map[string]any); ok {
				if candidate := mapString(nextAction, "url"); candidate != "" {
					nextActionCandidates = append(nextActionCandidates, candidate)
				}
				if redirect, ok := nextAction["redirect_to_url"].(map[string]any); ok {
					if candidate := mapString(redirect, "url"); candidate != "" {
						nextActionCandidates = append(nextActionCandidates, candidate)
					}
				}
			}
			for _, key := range []string{
				"redirect_url",
				"provider_redirect_url",
			} {
				candidate := mapString(typed, key)
				parsed, err := url.Parse(strings.TrimSpace(candidate))
				if err == nil && parsed != nil && parsed.Hostname() != "" &&
					(strings.EqualFold(parsed.Scheme, "http") ||
						strings.EqualFold(parsed.Scheme, "https")) {
					fallbackCandidates = append(fallbackCandidates, candidate)
				}
			}
			keys := make([]string, 0, len(typed))
			for key := range typed {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			for _, key := range keys {
				walk(typed[key], depth+1)
			}
		case []any:
			for _, child := range typed {
				walk(child, depth+1)
			}
		}
	}
	walk(value, 0)
	for _, candidate := range append(nextActionCandidates, fallbackCandidates...) {
		parsed, err := url.Parse(candidate)
		if err == nil && parsed != nil && parsed.Hostname() != "" {
			return candidate
		}
	}
	return ""
}

func (executor *Executor) pollAttachmentCSGoPayPaymentPage(
	ctx context.Context,
	client *http.Client,
	profile browserhttp.Profile,
	checkout checkoutSession,
	snapshot stripeSnapshot,
	policy *goPayAttachmentPollPolicy,
) (goPayAttachmentPollResult, error) {
	const operation = "Stripe GoPay Payment Page poll"
	if executor == nil {
		return goPayAttachmentPollResult{}, errors.New(operation + ": executor is missing")
	}
	if client == nil {
		return goPayAttachmentPollResult{}, errors.New(operation + ": HTTP client is missing")
	}
	if !validGoPayCheckoutSessionID(strings.TrimSpace(checkout.ID)) ||
		strings.TrimSpace(snapshot.PublishableKey) == "" {
		return goPayAttachmentPollResult{}, withUpstreamResponse(
			permanent(operation+": Stripe checkout context is incomplete"),
			snapshot.ResponseDiagnostic,
		)
	}
	if _, err := attachmentCSGoPayProfile(profile); err != nil {
		return goPayAttachmentPollResult{}, fmt.Errorf("%s: %w", operation, err)
	}
	maxAttempts, interval := normalizeAttachmentCSGoPayPollPolicy(policy)
	values := attachmentCSGoPayElementsValues(snapshot)
	values.Set("key", snapshot.PublishableKey)
	values.Set("_stripe_version", stripeVersion)

	result := goPayAttachmentPollResult{}
	responseDiagnostics := ""
	attemptFailures := make([]error, 0, 4)
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		if err := ctx.Err(); err != nil {
			return result, withUpstreamResponse(errors.Join(err, errors.Join(attemptFailures...)), responseDiagnostics)
		}
		response, err := executor.doAttachmentCSGoPayStripeFormResponse(
			ctx,
			client,
			http.MethodGet,
			"/v1/payment_pages/"+url.PathEscape(checkout.ID),
			values,
			goPayPaymentPageReadFieldOrder,
			profile,
			checkout,
			snapshot.PublishableKey,
			operation,
		)
		result.Attempts = attempt
		if err != nil {
			if diagnostic := safeUpstreamResponseDiagnostic(err); diagnostic != "" {
				responseDiagnostics = appendUpstreamResponseDiagnostic(responseDiagnostics, diagnostic)
			}
			attemptFailures = append(attemptFailures, fmt.Errorf("poll attempt %d: %s", attempt, err.Error()))
		} else {
			result.Response = response
			responseDiagnostics = appendUpstreamResponseDiagnostic(
				responseDiagnostics,
				response.ResponseDiagnostic,
			)
			if redirect := extractAttachmentCSGoPayRedirectURL(response.Payload); redirect != "" {
				result.Redirect = redirect
				return result, nil
			}
			if attachmentCSGoPayTerminalPollFailure(response.Payload) {
				failure := attachmentCSGoPayPollFailure(response.Payload)
				return result, withUpstreamResponse(
					fmt.Errorf(
						"%s: terminal failure: submission=%s error_code=%s payment_error=%s decline=%s",
						operation,
						fallbackLabel(attachmentCSGoPaySubmissionState(response.Payload), "<missing>"),
						fallbackLabel(failure.ErrorCode, "<missing>"),
						fallbackLabel(failure.PaymentErrorCode, "<missing>"),
						fallbackLabel(failure.DeclineCode, "<missing>"),
					),
					responseDiagnostics,
				)
			}
		}
		if attempt == maxAttempts {
			break
		}
		if err := waitContext(ctx, interval); err != nil {
			return result, withUpstreamResponse(
				errors.Join(err, errors.Join(attemptFailures...)),
				responseDiagnostics,
			)
		}
	}
	failure := attachmentCSGoPayPollFailure(result.Response.Payload)
	timeoutErr := fmt.Errorf(
		"%s: timeout after %d attempts: submission=%s error_code=%s payment_error=%s decline=%s",
		operation,
		result.Attempts,
		fallbackLabel(attachmentCSGoPaySubmissionState(result.Response.Payload), "<missing>"),
		fallbackLabel(failure.ErrorCode, "<missing>"),
		fallbackLabel(failure.PaymentErrorCode, "<missing>"),
		fallbackLabel(failure.DeclineCode, "<missing>"),
	)
	return result, withUpstreamResponse(
		errors.Join(timeoutErr, errors.Join(attemptFailures...)),
		responseDiagnostics,
	)
}

func resolveAttachmentCSGoPayProviderRedirect(
	ctx context.Context,
	client *http.Client,
	profile browserhttp.Profile,
	start string,
	policy *goPayAttachmentProviderRedirectPolicy,
) (string, error) {
	const operation = "GoPay provider redirect"
	if client == nil {
		return "", errors.New(operation + ": HTTP client is missing")
	}
	current := strings.TrimSpace(start)
	if current == "" {
		return "", errors.New(operation + ": URL is empty")
	}
	resolvedProfile, err := attachmentCSGoPayProfile(profile)
	if err != nil {
		return "", fmt.Errorf("%s: %w", operation, err)
	}
	maxHops, suffixes := normalizeAttachmentCSGoPayProviderPolicy(policy)

	clientCopy := *client
	clientCopy.Transport = preserveMalformedRedirectResponses(clientCopy.Transport)
	clientCopy.CheckRedirect = func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}
	responseDiagnostics := ""
	for hop := 0; ; hop++ {
		parsed, err := parseAttachmentCSGoPayProviderURL(current)
		if err != nil {
			return "", withUpstreamResponse(fmt.Errorf("%s: %w", operation, err), responseDiagnostics)
		}
		if attachmentCSGoPayProviderHostAllowed(parsed.Hostname(), suffixes) {
			return current, nil
		}
		if hop >= maxHops {
			return "", withUpstreamResponse(
				fmt.Errorf(
					"%s: did not reach an allowed host after %d hops; last_host=%s",
					operation,
					maxHops,
					parsed.Hostname(),
				),
				responseDiagnostics,
			)
		}

		request, err := http.NewRequestWithContext(ctx, http.MethodGet, current, nil)
		if err != nil {
			return "", withUpstreamResponse(fmt.Errorf("%s: create request: %w", operation, err), responseDiagnostics)
		}
		request.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
		request.Header.Set("Accept-Language", "id-ID,id;q=0.9,en;q=0.8")
		request.Header.Set("Referer", "https://checkout.stripe.com/")
		browserhttp.ApplyLowEntropyHeaders(request.Header, resolvedProfile)
		response, requestErr := doHTTPPreservingErrorResponse(&clientCopy, request)
		if requestErr != nil {
			requestErr = contextRequestError(ctx, operation, requestErr)
			requestErr = readHTTPResponseError(requestErr, response, operation)
			return "", withUpstreamResponse(requestErr, responseDiagnostics)
		}
		if response == nil {
			return "", withUpstreamResponse(errors.New(operation+": empty upstream response"), responseDiagnostics)
		}
		body, readErr := readAttachmentCSGoPayResponseBody(response)
		diagnostic := httpResponseDiagnostic(response, operation, body)
		responseDiagnostics = appendUpstreamResponseDiagnostic(responseDiagnostics, diagnostic)
		if readErr != nil {
			return "", withUpstreamResponse(
				fmt.Errorf("%s: read HTTP %d response: %w", operation, response.StatusCode, readErr),
				responseDiagnostics,
			)
		}
		if response.StatusCode < 300 || response.StatusCode > 399 {
			if candidate := goPayAttachmentMidtransHTMLURLPattern.Find(body); len(candidate) > 0 {
				midtransURL := string(candidate)
				midtrans, parseErr := parseAttachmentCSGoPayProviderURL(midtransURL)
				if parseErr == nil && attachmentCSGoPayProviderHostAllowed(
					midtrans.Hostname(),
					suffixes,
				) {
					return midtransURL, nil
				}
			}
			return "", withUpstreamResponse(
				fmt.Errorf(
					"%s: stopped at non-provider host=%s with HTTP %d",
					operation,
					parsed.Hostname(),
					response.StatusCode,
				),
				responseDiagnostics,
			)
		}
		location := strings.TrimSpace(response.Header.Get("Location"))
		if location == "" {
			return "", withUpstreamResponse(
				fmt.Errorf("%s: HTTP %d response is missing Location", operation, response.StatusCode),
				responseDiagnostics,
			)
		}
		next, err := url.Parse(location)
		if err != nil {
			return "", withUpstreamResponse(
				fmt.Errorf("%s: Location is invalid: %w", operation, err),
				responseDiagnostics,
			)
		}
		resolved := parsed.ResolveReference(next)
		if resolved == nil || resolved.Hostname() == "" {
			return "", withUpstreamResponse(
				errors.New(operation+": resolved redirect URL has no host"),
				responseDiagnostics,
			)
		}
		if !strings.EqualFold(resolved.Scheme, "http") && !strings.EqualFold(resolved.Scheme, "https") {
			return "", withUpstreamResponse(
				fmt.Errorf("%s: unsupported redirect scheme %s", operation, resolved.Scheme),
				responseDiagnostics,
			)
		}
		current = resolved.String()
	}
}

func (executor *Executor) doAttachmentCSGoPayStripeFormResponse(
	ctx context.Context,
	client *http.Client,
	method,
	path string,
	values url.Values,
	fieldOrder []string,
	profile browserhttp.Profile,
	checkout checkoutSession,
	publishableKey,
	operation string,
) (protocolJSONResponse, error) {
	if client == nil {
		return protocolJSONResponse{}, errors.New(operation + ": HTTP client is missing")
	}
	publishableKey = strings.TrimSpace(publishableKey)
	if publishableKey == "" || strings.ContainsAny(publishableKey, "\x00\r\n") {
		return protocolJSONResponse{}, errors.New(operation + ": publishable key is invalid")
	}
	encoded := encodeStripeForm(values, fieldOrder)
	endpoint := executor.config.StripeBaseURL + path
	var body io.Reader
	if method == http.MethodGet {
		if encoded != "" {
			endpoint += "?" + encoded
		}
	} else {
		body = strings.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, endpoint, body)
	if err != nil {
		return protocolJSONResponse{}, fmt.Errorf("%s: create request: %w", operation, err)
	}
	resolvedProfile, err := attachmentCSGoPayProfile(profile)
	if err != nil {
		return protocolJSONResponse{}, fmt.Errorf("%s: %w", operation, err)
	}
	browserhttp.ApplyLowEntropyHeaders(request.Header, resolvedProfile)
	request.Header.Set("Authorization", "Bearer "+publishableKey)
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Accept-Language", "id-ID,id;q=0.9,en;q=0.8")
	request.Header.Set("Origin", "https://checkout.stripe.com")
	request.Header.Set("Referer", attachmentCSGoPayCheckoutPageURL(checkout.ID))
	request.Header.Set("sec-fetch-site", "same-site")
	request.Header.Set("sec-fetch-mode", "cors")
	request.Header.Set("sec-fetch-dest", "empty")
	if method != http.MethodGet {
		request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	request = browserhttp.WithHeaderOrder(request, goPayAttachmentStripeHeaderOrder...)
	response, err := doHTTPPreservingErrorResponse(client, request)
	if err != nil {
		requestErr := contextRequestError(ctx, operation, err)
		return protocolJSONResponse{}, readHTTPResponseError(requestErr, response, operation)
	}
	return decodeAttachmentCSGoPayStripeResponse(response, operation)
}

func decodeAttachmentCSGoPayStripeResponse(
	response *http.Response,
	operation string,
) (protocolJSONResponse, error) {
	if response == nil {
		return protocolJSONResponse{}, errors.New(operation + ": empty upstream response")
	}
	body, readErr := readAttachmentCSGoPayResponseBody(response)
	diagnostic := httpResponseDiagnostic(response, operation, body)
	if readErr != nil {
		return protocolJSONResponse{}, withUpstreamResponse(
			&upstreamResponseReadError{
				Operation:  operation,
				StatusCode: response.StatusCode,
				Err:        readErr,
			},
			diagnostic,
		)
	}
	if response.StatusCode != http.StatusOK {
		return protocolJSONResponse{}, &upstreamHTTPError{
			Operation:          operation,
			StatusCode:         response.StatusCode,
			ContentType:        protocolContentType(response.Header.Get("Content-Type")),
			ResponseDiagnostic: diagnostic,
		}
	}
	payload := map[string]any{}
	if len(bytes.TrimSpace(body)) > 0 {
		var decoded map[string]any
		if json.Unmarshal(body, &decoded) == nil && decoded != nil {
			payload = decoded
		}
	}
	return protocolJSONResponse{
		Payload:            payload,
		RawJSON:            append([]byte(nil), body...),
		ResponseDiagnostic: diagnostic,
	}, nil
}

func readAttachmentCSGoPayResponseBody(response *http.Response) ([]byte, error) {
	if response == nil || response.Body == nil {
		return nil, nil
	}
	body, err := io.ReadAll(response.Body)
	closeErr := response.Body.Close()
	if closeErr != nil {
		err = errors.Join(err, closeErr)
	}
	return body, err
}

func attachmentCSGoPayElementsValues(snapshot stripeSnapshot) url.Values {
	values := url.Values{
		"elements_session_client[client_betas][0]":                        {"custom_checkout_server_updates_1"},
		"elements_session_client[client_betas][1]":                        {"custom_checkout_manual_approval_1"},
		"elements_session_client[elements_init_source]":                   {"custom_checkout"},
		"elements_session_client[referrer_host]":                          {"chatgpt.com"},
		"elements_session_client[locale]":                                 {"en"},
		"elements_session_client[is_aggregation_expected]":                {"false"},
		"elements_options_client[saved_payment_method][enable_save]":      {"auto"},
		"elements_options_client[saved_payment_method][enable_redisplay]": {"auto"},
	}
	if stripeJSID := strings.TrimSpace(snapshot.Context.StripeJSID); stripeJSID != "" {
		values.Set("elements_session_client[stripe_js_id]", stripeJSID)
	}
	if sessionID := strings.TrimSpace(snapshot.Context.ElementsSessionID); sessionID != "" {
		values.Set("elements_session_client[session_id]", sessionID)
	}
	return values
}

func (executor *Executor) attachmentCSGoPayConfirmReturnURL(checkout checkoutSession) string {
	processor := strings.TrimSpace(checkout.ProcessorEntity)
	if processor == "" {
		processor = goPayProcessorEntity
	}
	successURL := executor.config.ChatGPTBaseURL +
		"/backend-api/payments/checkout/" + url.PathEscape(processor) +
		"/" + url.PathEscape(checkout.ID) +
		"/success?billing_country=" + goPayCountry
	return attachmentCSGoPayCheckoutPageURL(checkout.ID) +
		"?returned_from_redirect=true&ui_mode=custom&return_url=" +
		url.QueryEscape(successURL)
}

func attachmentCSGoPayCheckoutPageURL(checkoutID string) string {
	return "https://checkout.stripe.com/c/pay/" + url.PathEscape(strings.TrimSpace(checkoutID))
}

func validateAttachmentCSGoPayBrowserIDs(ids goPayAttachmentBrowserIDs) error {
	if !attachmentCSGoPayUUID(ids.ClientSessionID) {
		return errors.New("client session ID is not UUID-shaped")
	}
	for _, field := range []struct {
		name  string
		value string
	}{
		{name: "guid", value: ids.GUID},
		{name: "muid", value: ids.MUID},
		{name: "sid", value: ids.SID},
	} {
		if len(field.value) != 44 ||
			!attachmentCSGoPayUUID(field.value[:36]) ||
			!attachmentCSGoPayHex(field.value[36:]) {
			return fmt.Errorf("%s does not match the attachment browser ID format", field.name)
		}
	}
	return nil
}

func attachmentCSGoPayUUID(value string) bool {
	if len(value) != 36 || value[8] != '-' || value[13] != '-' ||
		value[18] != '-' || value[23] != '-' {
		return false
	}
	for index, character := range value {
		if index == 8 || index == 13 || index == 18 || index == 23 {
			continue
		}
		if character < '0' || character > '9' &&
			(character < 'a' || character > 'f') &&
			(character < 'A' || character > 'F') {
			return false
		}
	}
	return true
}

func attachmentCSGoPayHex(value string) bool {
	if value == "" {
		return false
	}
	for _, character := range value {
		if character < '0' || character > '9' &&
			(character < 'a' || character > 'f') &&
			(character < 'A' || character > 'F') {
			return false
		}
	}
	return true
}

func attachmentCSGoPayExpectedAmount(snapshot stripeSnapshot) string {
	return firstNonEmptyAttachmentCSGoPayValue(
		snapshot.Context.Amount,
		attachmentCSGoPayObservedAmount(snapshot.Payload),
		snapshot.Amount,
		"0",
	)
}

func attachmentCSGoPayObservedAmount(payload map[string]any) string {
	if options, ok := payload["elements_options"].(map[string]any); ok {
		if amount := strings.TrimSpace(scalarString(options["amount"])); amount != "" {
			return amount
		}
	}
	for _, path := range [][]string{
		{"total_summary", "due"},
		{"invoice", "amount_due"},
		{"invoice", "total"},
		{"checkout_state", "total", "total", "minorUnitsAmount"},
		{"checkout_state", "total", "minorUnitsAmount"},
	} {
		var current any = payload
		for _, key := range path {
			object, ok := current.(map[string]any)
			if !ok {
				current = nil
				break
			}
			current = object[key]
		}
		if amount := strings.TrimSpace(scalarString(current)); amount != "" {
			return amount
		}
	}
	if lineItems, ok := payload["line_items"].([]any); ok {
		var total int64
		found := false
		for _, item := range lineItems {
			object, ok := item.(map[string]any)
			if !ok {
				continue
			}
			amount, err := strconv.ParseInt(strings.TrimSpace(scalarString(object["amount"])), 10, 64)
			if err != nil || amount > 0 && total > int64(^uint64(0)>>1)-amount ||
				amount < 0 && total < -int64(^uint64(0)>>1)-1-amount {
				continue
			}
			total += amount
			found = true
		}
		if found {
			return strconv.FormatInt(total, 10)
		}
	}
	return ""
}

func attachmentCSGoPayLinkBrand(payload map[string]any) string {
	if brand := explicitAttachmentCSGoPayLinkBrand(payload); brand != "" {
		return brand
	}
	return "link"
}

func explicitAttachmentCSGoPayLinkBrand(payload map[string]any) string {
	if settings, ok := payload["link_settings"].(map[string]any); ok {
		if brand := strings.TrimSpace(mapString(settings, "link_brand")); brand != "" {
			return brand
		}
	}
	return ""
}

// withAttachmentCSGoPayLinkBrand carries the last non-empty Stripe link brand
// across attachment-style init/refresh snapshots. stripeContext predates that
// field, so the attachment flow keeps the value in the payload consumed by
// confirm without changing the shared context used by unrelated methods.
func withAttachmentCSGoPayLinkBrand(snapshot stripeSnapshot, linkBrand string) stripeSnapshot {
	linkBrand = strings.TrimSpace(linkBrand)
	if linkBrand == "" {
		return snapshot
	}
	payload := make(map[string]any, len(snapshot.Payload)+1)
	for key, value := range snapshot.Payload {
		payload[key] = value
	}
	settings := map[string]any{}
	if current, ok := snapshot.Payload["link_settings"].(map[string]any); ok {
		for key, value := range current {
			settings[key] = value
		}
	}
	settings["link_brand"] = linkBrand
	payload["link_settings"] = settings
	snapshot.Payload = payload
	return snapshot
}

func withAttachmentCSGoPayLinkBrandFallback(
	snapshot stripeSnapshot,
	fallback string,
) stripeSnapshot {
	if explicitAttachmentCSGoPayLinkBrand(snapshot.Payload) != "" {
		return snapshot
	}
	return withAttachmentCSGoPayLinkBrand(snapshot, fallback)
}

func attachmentCSGoPaySnapshotFromResponse(
	previous stripeSnapshot,
	response protocolJSONResponse,
) stripeSnapshot {
	current := previous.Context
	if value := strings.TrimSpace(mapString(response.Payload, "elements_session_id")); value != "" {
		current.ElementsSessionID = value
	}
	if value := strings.TrimSpace(mapString(response.Payload, "config_id")); value != "" {
		current.ConfigID = value
	}
	if value := strings.TrimSpace(mapString(response.Payload, "init_checksum")); value != "" {
		current.InitChecksum = value
	}
	if value := strings.ToLower(strings.TrimSpace(mapString(response.Payload, "currency"))); value != "" {
		current.Currency = value
	}
	amount := attachmentCSGoPayObservedAmount(response.Payload)
	if amount == "" {
		amount = previous.Amount
	}
	current.Amount = firstNonEmptyAttachmentCSGoPayValue(amount, current.Amount)
	methods := availablePaymentMethods(response.Payload)
	if len(methods) == 0 {
		methods = append([]string(nil), previous.Methods...)
	}
	pricing := authoritativeStripePricing(response.Payload, current.Currency)
	if pricing == nil {
		pricing = previous.Pricing
	}
	return stripeSnapshot{
		Payload:            response.Payload,
		Context:            current,
		HostedURL:          previous.HostedURL,
		Amount:             amount,
		Pricing:            pricing,
		Methods:            methods,
		PublishableKey:     previous.PublishableKey,
		ResponseDiagnostic: appendUpstreamResponseDiagnostic(previous.ResponseDiagnostic, response.ResponseDiagnostic),
	}
}

func firstNonEmptyAttachmentCSGoPayValue(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

type goPayAttachmentPollFailure struct {
	ErrorCode        string
	PaymentErrorCode string
	DeclineCode      string
}

func attachmentCSGoPayPollFailure(payload map[string]any) goPayAttachmentPollFailure {
	failure := goPayAttachmentPollFailure{}
	if attempt, ok := payload["submission_attempt"].(map[string]any); ok {
		if object, ok := attempt["error"].(map[string]any); ok {
			failure.ErrorCode = strings.TrimSpace(mapString(object, "code"))
			failure.DeclineCode = strings.TrimSpace(mapString(object, "decline_code"))
			if paymentError, ok := object["payment_error"].(map[string]any); ok {
				failure.PaymentErrorCode = strings.TrimSpace(mapString(paymentError, "code"))
				if decline := strings.TrimSpace(mapString(paymentError, "decline_code")); decline != "" {
					failure.DeclineCode = decline
				}
			}
		}
	}
	for _, key := range []string{"payment_intent", "setup_intent"} {
		intent, ok := payload[key].(map[string]any)
		if !ok {
			continue
		}
		for _, errorKey := range []string{"last_payment_error", "last_setup_error"} {
			object, ok := intent[errorKey].(map[string]any)
			if !ok {
				continue
			}
			if failure.PaymentErrorCode == "" {
				failure.PaymentErrorCode = strings.TrimSpace(mapString(object, "code"))
			}
			if failure.DeclineCode == "" {
				failure.DeclineCode = strings.TrimSpace(mapString(object, "decline_code"))
			}
		}
	}
	return failure
}

func attachmentCSGoPaySubmissionState(payload map[string]any) string {
	if attempt, ok := payload["submission_attempt"].(map[string]any); ok {
		return strings.TrimSpace(mapString(attempt, "state"))
	}
	return ""
}

func attachmentCSGoPayTerminalPollFailure(payload map[string]any) bool {
	failure := attachmentCSGoPayPollFailure(payload)
	text := strings.ToLower(strings.Join([]string{
		failure.ErrorCode,
		failure.PaymentErrorCode,
		failure.DeclineCode,
		mapString(payload, "status"),
		mapString(payload, "payment_status"),
	}, " "))
	for _, marker := range []string{
		"generic_decline",
		"setup_attempt_failed",
		"requires_payment_method",
		"canceled",
		"expired",
	} {
		if strings.Contains(text, marker) {
			return true
		}
	}
	return false
}

func normalizeAttachmentCSGoPayPollPolicy(policy *goPayAttachmentPollPolicy) (int, time.Duration) {
	if policy == nil {
		return goPayAttachmentPollDefaultMaxAttempts, goPayAttachmentPollDefaultInterval
	}
	maxAttempts := policy.MaxAttempts
	if maxAttempts < 1 {
		maxAttempts = 1
	}
	interval := policy.Interval
	if interval < 0 {
		interval = 0
	}
	return maxAttempts, interval
}

func normalizeAttachmentCSGoPayProviderPolicy(
	policy *goPayAttachmentProviderRedirectPolicy,
) (int, []string) {
	maxHops := goPayAttachmentProviderDefaultMaxHops
	suffixes := append([]string(nil), goPayAttachmentProviderHostSuffixes...)
	if policy != nil {
		maxHops = policy.MaxHops
		if maxHops < 1 {
			maxHops = 1
		}
		if maxHops > goPayAttachmentProviderMaximumHops {
			maxHops = goPayAttachmentProviderMaximumHops
		}
		if policy.AllowedHostSuffixes != nil {
			suffixes = append([]string(nil), policy.AllowedHostSuffixes...)
		}
	}
	for index, suffix := range suffixes {
		suffixes[index] = strings.ToLower(strings.TrimSuffix(strings.TrimSpace(suffix), "."))
	}
	return maxHops, suffixes
}

func parseAttachmentCSGoPayProviderURL(value string) (*url.URL, error) {
	parsed, err := url.Parse(strings.TrimSpace(value))
	if err != nil || parsed == nil || parsed.Hostname() == "" {
		return nil, errors.New("provider redirect URL is invalid")
	}
	if !strings.EqualFold(parsed.Scheme, "http") && !strings.EqualFold(parsed.Scheme, "https") {
		return nil, fmt.Errorf("unsupported provider redirect scheme %s", parsed.Scheme)
	}
	return parsed, nil
}

func attachmentCSGoPayProviderHostAllowed(host string, suffixes []string) bool {
	host = strings.ToLower(strings.TrimSuffix(strings.TrimSpace(host), "."))
	for _, suffix := range suffixes {
		if suffix != "" && (host == suffix || strings.HasSuffix(host, "."+suffix)) {
			return true
		}
	}
	return false
}

func attachmentCSGoPayProfile(profile browserhttp.Profile) (browserhttp.Profile, error) {
	resolved, ok := canonicalGoPayBrowserProfile(profile)
	if !ok {
		return browserhttp.Profile{}, errors.New("browser profile is not a registered GoPay profile")
	}
	return resolved, nil
}
