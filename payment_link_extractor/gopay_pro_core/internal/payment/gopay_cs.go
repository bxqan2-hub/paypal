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
	"strconv"
	"strings"

	"go-chatgpt/internal/browserhttp"
	"go-chatgpt/internal/domain"
	"go-chatgpt/internal/gopayaddress"
	"go-chatgpt/internal/jobs"
)

const (
	goPayCountry          = "ID"
	goPayCurrency         = "IDR"
	goPayProcessorEntity  = "openai_llc"
	goPayCheckoutProvider = "stripe"

	goPayStripeRuntimeVersion   = "4cd120cf9f"
	goPayStripeRuntimeTimestamp = "2024-01-01 00:00:00 -0000"
	goPayStripeRuntimeRevision  = "4cd120cf9fecc5fbf8acb8a2376cf340742bb8b2"
	goPayStripeSchemaRevision   = "e698631b472aab85b018db0d5236c044b5b95deb4b2c33a375af83ab45375770"
	goPayElementsLocale         = "id"

	goPayElementsPath     = "/v1/elements/sessions"
	goPayTaxesPath        = "/backend-api/payments/checkout/taxes"
	goPayCheckoutSnapshot = "/backend-api/payments/checkout/snapshot"
)

var goPayElementsFieldOrder = []string{
	"client_betas[0]",
	"client_betas[1]",
	"deferred_intent[mode]",
	"deferred_intent[amount]",
	"deferred_intent[currency]",
	"deferred_intent[setup_future_usage]",
	"deferred_intent[payment_method_types][0]",
	"deferred_intent[payment_method_types][1]",
	"deferred_intent[payment_method_types][2]",
	"currency",
	"key",
	"_stripe_version",
	"elements_init_source",
	"referrer_host",
	"stripe_js_id",
	"locale",
	"type",
	"checkout_session_id",
}

var goPayTaxRegionFieldOrder = []string{
	"tax_region[country]",
	"tax_region[line1]",
	"tax_region[city]",
	"tax_region[postal_code]",
	"tax_region[state]",
	"elements_session_client[client_betas][0]",
	"elements_session_client[client_betas][1]",
	"elements_session_client[elements_init_source]",
	"elements_session_client[referrer_host]",
	"elements_session_client[session_id]",
	"elements_session_client[stripe_js_id]",
	"elements_session_client[locale]",
	"elements_session_client[is_aggregation_expected]",
	"elements_options_client[saved_payment_method][enable_save]",
	"elements_options_client[saved_payment_method][enable_redisplay]",
	"client_attribution_metadata[merchant_integration_additional_elements][0]",
	"client_attribution_metadata[merchant_integration_additional_elements][1]",
	"client_attribution_metadata[merchant_integration_additional_elements][2]",
	"key",
	"_stripe_version",
}

var goPayPaymentPageReadFieldOrder = []string{
	"elements_session_client[client_betas][0]",
	"elements_session_client[client_betas][1]",
	"elements_session_client[elements_init_source]",
	"elements_session_client[referrer_host]",
	"elements_session_client[session_id]",
	"elements_session_client[stripe_js_id]",
	"elements_session_client[locale]",
	"elements_session_client[is_aggregation_expected]",
	"elements_options_client[saved_payment_method][enable_save]",
	"elements_options_client[saved_payment_method][enable_redisplay]",
	"key",
	"_stripe_version",
}

var goPayConfirmFieldOrder = []string{
	"guid",
	"muid",
	"sid",
	"payment_method_data[billing_details][name]",
	"payment_method_data[billing_details][email]",
	"payment_method_data[billing_details][address][line1]",
	"payment_method_data[billing_details][address][city]",
	"payment_method_data[billing_details][address][postal_code]",
	"payment_method_data[billing_details][address][state]",
	"payment_method_data[billing_details][address][country]",
	"payment_method_data[type]",
	"payment_method_data[payment_user_agent]",
	"payment_method_data[referrer]",
	"payment_method_data[time_on_page]",
	"payment_method_data[client_attribution_metadata][client_session_id]",
	"payment_method_data[client_attribution_metadata][checkout_session_id]",
	"payment_method_data[client_attribution_metadata][merchant_integration_source]",
	"payment_method_data[client_attribution_metadata][merchant_integration_subtype]",
	"payment_method_data[client_attribution_metadata][merchant_integration_version]",
	"payment_method_data[client_attribution_metadata][payment_intent_creation_flow]",
	"payment_method_data[client_attribution_metadata][payment_method_selection_flow]",
	"payment_method_data[client_attribution_metadata][elements_session_id]",
	"payment_method_data[client_attribution_metadata][elements_session_config_id]",
	"payment_method_data[client_attribution_metadata][checkout_config_id]",
	"payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]",
	"payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]",
	"payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][2]",
	"init_checksum",
	"version",
	"expected_amount",
	"js_checksum",
	"rv_timestamp",
	"expected_payment_method_type",
	"return_url",
	"elements_session_client[client_betas][0]",
	"elements_session_client[client_betas][1]",
	"elements_session_client[elements_init_source]",
	"elements_session_client[referrer_host]",
	"elements_session_client[session_id]",
	"elements_session_client[stripe_js_id]",
	"elements_session_client[locale]",
	"elements_session_client[is_aggregation_expected]",
	"elements_options_client[saved_payment_method][enable_save]",
	"elements_options_client[saved_payment_method][enable_redisplay]",
	"client_attribution_metadata[client_session_id]",
	"client_attribution_metadata[checkout_session_id]",
	"client_attribution_metadata[merchant_integration_source]",
	"client_attribution_metadata[merchant_integration_version]",
	"client_attribution_metadata[merchant_integration_subtype]",
	"client_attribution_metadata[merchant_integration_additional_elements][0]",
	"client_attribution_metadata[merchant_integration_additional_elements][1]",
	"client_attribution_metadata[merchant_integration_additional_elements][2]",
	"client_attribution_metadata[payment_intent_creation_flow]",
	"client_attribution_metadata[payment_method_selection_flow]",
	"client_attribution_metadata[elements_session_id]",
	"client_attribution_metadata[elements_session_config_id]",
	"client_attribution_metadata[checkout_config_id]",
	"link_brand",
	"key",
	"_stripe_version",
}

// GoPay's Stripe requests are fetch/CORS requests. The generic Stripe helper
// still models a navigation-shaped client and therefore includes
// Upgrade-Insecure-Requests and Sec-Fetch-User; neither header is present in
// the captured GoPay Elements, tax, Payment Page, or confirm requests.
var goPayStripeFetchRequestHeaderOrder = []string{
	"sec-ch-ua",
	"sec-ch-ua-mobile",
	"sec-ch-ua-platform",
	"user-agent",
	"accept",
	"sec-fetch-site",
	"sec-fetch-mode",
	"sec-fetch-dest",
	"accept-encoding",
	"accept-language",
	"priority",
	"content-type",
	"origin",
	"referer",
}

// canonicalCSGoPayCheckout keeps the Indonesia GoPay flow bound to the
// standard Stripe Checkout family. A hosted-page field is never accepted as
// a substitute for the required cs_ Checkout identifier.
func canonicalCSGoPayCheckout(checkout checkoutSession) (checkoutSession, error) {
	checkoutID := strings.TrimSpace(checkout.ID)
	hostedPageID := strings.TrimSpace(checkout.HostedPageID)
	if hostedPageID != "" {
		return checkoutSession{}, withUpstreamResponse(
			permanent("GoPay CS checkout cannot contain a hosted page identifier"),
			checkout.ResponseDiagnostic,
		)
	}
	if !validGoPayCheckoutSessionID(checkoutID) {
		return checkoutSession{}, withUpstreamResponse(
			permanent("GoPay checkout did not return a Stripe CS session (cs_/cs_test_/cs_live_)"),
			checkout.ResponseDiagnostic,
		)
	}

	country := strings.ToUpper(strings.TrimSpace(checkout.Country))
	if country == "" {
		country = goPayCountry
	}
	currency := strings.ToUpper(strings.TrimSpace(checkout.Currency))
	if currency == "" {
		currency = goPayCurrency
	}
	if country != goPayCountry || currency != goPayCurrency {
		return checkoutSession{}, withUpstreamResponse(
			permanent(fmt.Sprintf(
				"GoPay CS checkout market changed to %s/%s, want %s/%s",
				fallbackLabel(country, "<missing>"),
				fallbackLabel(currency, "<missing>"),
				goPayCountry,
				goPayCurrency,
			)),
			checkout.ResponseDiagnostic,
		)
	}

	provider := strings.TrimSpace(checkout.CheckoutProvider)
	if provider == "" {
		provider = goPayCheckoutProvider
	}

	processor := strings.TrimSpace(checkout.ProcessorEntity)
	if processor == "" {
		processor = goPayProcessorEntity
	}

	checkout.ID = checkoutID
	checkout.HostedPageID = ""
	checkout.Country = country
	checkout.Currency = currency
	checkout.CheckoutProvider = provider
	checkout.ProcessorEntity = processor
	return checkout, nil
}

// stripeInitCSGoPay performs the shared Stripe Payment Page init and then
// applies the GoPay-specific fail-closed capability and identity gates seen in
// the browser capture.
func (executor *Executor) stripeInitCSGoPay(
	ctx context.Context,
	client *http.Client,
	checkout checkoutSession,
	current stripeContext,
) (stripeSnapshot, error) {
	canonical, err := canonicalCSGoPayCheckout(checkout)
	if err != nil {
		return stripeSnapshot{}, err
	}
	current.BrowserLocale = "id-ID"
	current.ElementsLocale = "id-ID"
	current.SavedPaymentMethodMode = "auto"
	if err := current.setBrowserTimeZone("Asia/Jakarta", current.browserTimeZonePersisted); err != nil {
		return stripeSnapshot{}, fmt.Errorf("prepare GoPay Stripe time zone: %w", err)
	}

	snapshot, err := executor.stripeInitWithFormResponse(
		ctx,
		client,
		canonical,
		current,
		func(
			requestContext context.Context,
			requestClient *http.Client,
			method,
			path string,
			values url.Values,
			operation string,
		) (protocolJSONResponse, error) {
			return executor.doGoPayStripeFormResponse(
				requestContext,
				requestClient,
				method,
				path,
				values,
				stripeInitFieldOrder,
				operation,
			)
		},
	)
	if err != nil {
		return stripeSnapshot{}, err
	}
	if err := validateCSGoPayStripeInit(snapshot, canonical.ID); err != nil {
		return stripeSnapshot{}, err
	}
	return snapshot, nil
}

func validateCSGoPayStripeInit(snapshot stripeSnapshot, expectedID string) error {
	fail := func(err error) error {
		return withUpstreamResponse(permanentWrap("Stripe GoPay init contract", err), snapshot.ResponseDiagnostic)
	}
	if err := validateCSGoPayStripeInitBinding(snapshot, expectedID); err != nil {
		return fail(err)
	}
	rawCurrency, err := goPayRequiredTopLevelString(snapshot.Payload, "currency")
	if err != nil {
		return fail(err)
	}
	currency := strings.ToUpper(rawCurrency)
	if currency != goPayCurrency ||
		strings.ToUpper(strings.TrimSpace(snapshot.Context.Currency)) != goPayCurrency {
		return fail(fmt.Errorf(
			"currency changed: response=%s context=%s want=%s",
			fallbackLabel(currency, "<missing>"),
			fallbackLabel(strings.ToUpper(strings.TrimSpace(snapshot.Context.Currency)), "<missing>"),
			goPayCurrency,
		))
	}
	if !containsMethod(snapshot.Methods, "gopay") {
		return fail(fmt.Errorf(
			"gopay is unavailable (available=%s)",
			stripeMethodsSummary(snapshot.Methods),
		))
	}
	if country := goPayStripeGeocodingCountry(snapshot.Payload); country != "" && country != goPayCountry {
		return fail(fmt.Errorf("geocoding country changed to %s, want %s", country, goPayCountry))
	}
	return nil
}

// validateCSGoPayStripeInitBinding validates the Payment Page identity and the
// config/checksum pair without requiring business fields. The attachment's
// first init establishes this anchor; later init responses may omit these
// fields, but they must never replace it with a different page/session or
// disagree with the carried Stripe context.
func validateCSGoPayStripeInitBinding(snapshot stripeSnapshot, expectedID string) error {
	if !validGoPayCheckoutSessionID(strings.TrimSpace(expectedID)) {
		return errors.New("checkout session identity is unavailable")
	}
	pageID, err := goPayRequiredTopLevelString(snapshot.Payload, "id")
	if err != nil {
		return err
	}
	if !validCheckoutSessionIDWithPrefix(pageID, "ppage_") {
		return errors.New("top-level payment page id is missing or invalid")
	}
	sessionID, err := goPayRequiredTopLevelString(snapshot.Payload, "session_id")
	if err != nil {
		return err
	}
	if sessionID != expectedID {
		return fmt.Errorf(
			"checkout session changed: expected=%s actual=%s",
			expectedID,
			fallbackLabel(sessionID, "<missing>"),
		)
	}
	configID, err := goPayRequiredTopLevelString(snapshot.Payload, "config_id")
	if err != nil {
		return err
	}
	if configID == "" || configID != strings.TrimSpace(snapshot.Context.ConfigID) {
		return errors.New("top-level config_id is missing or disagrees with Stripe context")
	}
	initChecksum, err := goPayRequiredTopLevelString(snapshot.Payload, "init_checksum")
	if err != nil {
		return err
	}
	if initChecksum == "" || initChecksum != strings.TrimSpace(snapshot.Context.InitChecksum) {
		return errors.New("top-level init_checksum is missing or disagrees with Stripe context")
	}
	return nil
}

func goPayRequiredTopLevelString(payload map[string]any, key string) (string, error) {
	raw, exists := payload[key]
	if !exists {
		return "", fmt.Errorf("top-level %s is missing", key)
	}
	value, ok := raw.(string)
	if !ok || value == "" || value != strings.TrimSpace(value) ||
		strings.ContainsAny(value, "\x00\r\n") {
		return "", fmt.Errorf("top-level %s is invalid", key)
	}
	return value, nil
}

func goPayStripeGeocodingCountry(payload map[string]any) string {
	for _, path := range [][]string{
		{"geocoding", "country_code"},
		{"geocoding", "country"},
		{"tax_context", "geocoding", "country_code"},
		{"tax_context", "geocoding", "country"},
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
		if country := strings.ToUpper(strings.TrimSpace(scalarString(current))); country != "" {
			return country
		}
	}
	return ""
}

type goPayElementsSession struct {
	SessionID          string
	ConfigID           string
	ResponseDiagnostic string
}

func (executor *Executor) fetchCSGoPayElementsSession(
	ctx context.Context,
	client *http.Client,
	checkout checkoutSession,
	snapshot stripeSnapshot,
) (goPayElementsSession, error) {
	fail := func(err error, diagnostics ...string) (goPayElementsSession, error) {
		failure := withUpstreamResponse(permanentWrap("Stripe GoPay Elements contract", err), snapshot.ResponseDiagnostic)
		for _, diagnostic := range diagnostics {
			failure = withUpstreamResponse(failure, diagnostic)
		}
		return goPayElementsSession{}, failure
	}
	if client == nil {
		return fail(errors.New("HTTP client is missing"))
	}
	if !validGoPayCheckoutSessionID(checkout.ID) ||
		strings.TrimSpace(snapshot.Amount) == "" ||
		strings.TrimSpace(snapshot.PublishableKey) == "" ||
		strings.TrimSpace(snapshot.Context.StripeJSID) == "" {
		return fail(errors.New("authoritative Stripe context is incomplete"))
	}
	values := url.Values{
		"client_betas[0]":                          {"custom_checkout_server_updates_1"},
		"client_betas[1]":                          {"custom_checkout_manual_approval_1"},
		"deferred_intent[mode]":                    {"subscription"},
		"deferred_intent[amount]":                  {snapshot.Amount},
		"deferred_intent[currency]":                {"idr"},
		"deferred_intent[setup_future_usage]":      {"off_session"},
		"deferred_intent[payment_method_types][0]": {"card"},
		"deferred_intent[payment_method_types][1]": {"link"},
		"deferred_intent[payment_method_types][2]": {"gopay"},
		"currency":             {"idr"},
		"key":                  {snapshot.PublishableKey},
		"_stripe_version":      {stripeVersion},
		"elements_init_source": {"custom_checkout"},
		"referrer_host":        {"chatgpt.com"},
		"stripe_js_id":         {snapshot.Context.StripeJSID},
		"locale":               {goPayElementsLocale},
		"type":                 {"deferred_intent"},
		"checkout_session_id":  {checkout.ID},
	}
	response, err := executor.doGoPayStripeFormResponse(
		ctx,
		client,
		http.MethodGet,
		goPayElementsPath,
		values,
		goPayElementsFieldOrder,
		"Stripe GoPay Elements session",
	)
	if err != nil {
		return goPayElementsSession{}, err
	}

	sessionID, parseErr := goPayRequiredTopLevelString(response.Payload, "session_id")
	if parseErr != nil || !validCheckoutSessionIDWithPrefix(sessionID, "elements_session_") {
		if parseErr == nil {
			parseErr = errors.New("top-level session_id is not an Elements session")
		}
		return fail(parseErr, response.ResponseDiagnostic)
	}
	configID, parseErr := goPayRequiredTopLevelString(response.Payload, "config_id")
	if parseErr != nil {
		return fail(parseErr, response.ResponseDiagnostic)
	}
	methods, parseErr := goPayStringList(
		response.Payload["ordered_payment_method_types_and_wallets"],
		"ordered_payment_method_types_and_wallets",
	)
	if parseErr != nil || !containsMethod(methods, "gopay") {
		if parseErr == nil {
			parseErr = fmt.Errorf("gopay is unavailable (available=%s)", stripeMethodsSummary(methods))
		}
		return fail(parseErr, response.ResponseDiagnostic)
	}
	rawPreference, exists := response.Payload["payment_method_preference"]
	preference, ok := rawPreference.(map[string]any)
	if !exists || !ok {
		return fail(errors.New("payment_method_preference is missing or invalid"), response.ResponseDiagnostic)
	}
	rawCountry, present := preference["country_code"]
	country, ok := rawCountry.(string)
	country = strings.ToUpper(strings.TrimSpace(country))
	if !present || !ok || country != goPayCountry {
		return fail(fmt.Errorf(
			"payment_method_preference country changed to %s, want %s",
			fallbackLabel(country, "<invalid>"),
			goPayCountry,
		), response.ResponseDiagnostic)
	}
	return goPayElementsSession{
		SessionID:          sessionID,
		ConfigID:           configID,
		ResponseDiagnostic: response.ResponseDiagnostic,
	}, nil
}

func goPayStringList(value any, label string) ([]string, error) {
	var values []any
	switch typed := value.(type) {
	case []any:
		values = typed
	case []string:
		values = make([]any, 0, len(typed))
		for _, item := range typed {
			values = append(values, item)
		}
	default:
		return nil, fmt.Errorf("%s is missing or invalid", label)
	}
	result := make([]string, 0, len(values))
	seen := make(map[string]struct{}, len(values))
	for _, raw := range values {
		item, ok := raw.(string)
		item = strings.ToLower(strings.TrimSpace(item))
		if !ok || item == "" {
			return nil, fmt.Errorf("%s contains an invalid value", label)
		}
		if _, exists := seen[item]; exists {
			continue
		}
		seen[item] = struct{}{}
		result = append(result, item)
	}
	if len(result) == 0 {
		return nil, fmt.Errorf("%s is empty", label)
	}
	return result, nil
}

func (executor *Executor) doGoPayStripeFormResponse(
	ctx context.Context,
	client *http.Client,
	method,
	path string,
	values url.Values,
	preferred []string,
	operation string,
) (protocolJSONResponse, error) {
	if client == nil {
		return protocolJSONResponse{}, errors.New(operation + ": HTTP client is missing")
	}
	encoded := encodeStripeForm(values, preferred)
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
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Accept-Language", stripeAcceptLanguage(ctx))
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	request.Header.Set("Origin", "https://js.stripe.com")
	request.Header.Set("Referer", "https://js.stripe.com/")
	request.Header.Set("priority", "u=1, i")
	request.Header.Set("sec-fetch-dest", "empty")
	request.Header.Set("sec-fetch-mode", "cors")
	request.Header.Set("sec-fetch-site", "same-site")
	request = browserhttp.WithHeaderOrder(request, goPayStripeFetchRequestHeaderOrder...)
	response, err := client.Do(request)
	if err != nil {
		requestErr := contextRequestError(ctx, operation, err)
		return protocolJSONResponse{}, readHTTPResponseError(requestErr, response, operation)
	}
	return decodeJSONResponseDetails(response, operation)
}

func goPayElementsClientValues(snapshot stripeSnapshot, includeAttribution bool) url.Values {
	values := url.Values{
		"elements_session_client[client_betas][0]":                        {"custom_checkout_server_updates_1"},
		"elements_session_client[client_betas][1]":                        {"custom_checkout_manual_approval_1"},
		"elements_session_client[elements_init_source]":                   {"custom_checkout"},
		"elements_session_client[referrer_host]":                          {"chatgpt.com"},
		"elements_session_client[session_id]":                             {snapshot.Context.ElementsSessionID},
		"elements_session_client[stripe_js_id]":                           {snapshot.Context.StripeJSID},
		"elements_session_client[locale]":                                 {goPayElementsLocale},
		"elements_session_client[is_aggregation_expected]":                {"false"},
		"elements_options_client[saved_payment_method][enable_save]":      {"auto"},
		"elements_options_client[saved_payment_method][enable_redisplay]": {"auto"},
		"key":             {snapshot.PublishableKey},
		"_stripe_version": {stripeVersion},
	}
	if includeAttribution {
		values.Set(
			"client_attribution_metadata[merchant_integration_additional_elements][0]",
			"expressCheckout",
		)
		values.Set(
			"client_attribution_metadata[merchant_integration_additional_elements][1]",
			"payment",
		)
		values.Set(
			"client_attribution_metadata[merchant_integration_additional_elements][2]",
			"address",
		)
	}
	return values
}

func (executor *Executor) updateCSGoPayTaxRegion(
	ctx context.Context,
	client *http.Client,
	checkout checkoutSession,
	snapshot stripeSnapshot,
	billing billingDetails,
	includeState bool,
	expectedPageID,
	initialChecksum,
	expectedHostedURL string,
) (stripeSnapshot, error) {
	fieldCount := 4
	if includeState {
		fieldCount = 5
	}
	return executor.updateCSGoPayTaxRegionFields(
		ctx,
		client,
		checkout,
		snapshot,
		billing,
		fieldCount,
		expectedPageID,
		initialChecksum,
		expectedHostedURL,
	)
}

// updateCSGoPayTaxRegionFields mirrors Stripe Elements' progressive address
// updates. The capture submits country, then line1, then city, then postal
// code; the final state update is sent only after ChatGPT taxes/snapshot and
// the authoritative Payment Page refresh.
func (executor *Executor) updateCSGoPayTaxRegionFields(
	ctx context.Context,
	client *http.Client,
	checkout checkoutSession,
	snapshot stripeSnapshot,
	billing billingDetails,
	fieldCount int,
	expectedPageID,
	initialChecksum,
	expectedHostedURL string,
) (stripeSnapshot, error) {
	if fieldCount < 1 || fieldCount > 5 {
		return stripeSnapshot{}, errors.New("Stripe GoPay tax region field count is invalid")
	}
	values := goPayElementsClientValues(snapshot, true)
	values.Set("tax_region[country]", billing.Country)
	if fieldCount >= 2 {
		values.Set("tax_region[line1]", billing.Line1)
	}
	if fieldCount >= 3 {
		values.Set("tax_region[city]", billing.City)
	}
	if fieldCount >= 4 {
		values.Set("tax_region[postal_code]", billing.PostalCode)
	}
	if fieldCount >= 5 {
		values.Set("tax_region[state]", billing.State)
	}
	response, err := executor.doGoPayStripeFormResponse(
		ctx,
		client,
		http.MethodPost,
		"/v1/payment_pages/"+url.PathEscape(checkout.ID),
		values,
		goPayTaxRegionFieldOrder,
		fmt.Sprintf("Stripe GoPay tax region update (%d fields)", fieldCount),
	)
	if err != nil {
		return stripeSnapshot{}, err
	}
	return goPaySnapshotFromPaymentPage(
		snapshot,
		response,
		checkout.ID,
		expectedPageID,
		initialChecksum,
		expectedHostedURL,
		"",
		fmt.Sprintf("tax_region update (%d fields)", fieldCount),
	)
}

func (executor *Executor) refreshCSGoPayPaymentPage(
	ctx context.Context,
	client *http.Client,
	checkout checkoutSession,
	snapshot stripeSnapshot,
	expectedPageID,
	initialChecksum,
	expectedHostedURL,
	expectedConfigID,
	operation string,
) (stripeSnapshot, error) {
	response, err := executor.doGoPayStripeFormResponse(
		ctx,
		client,
		http.MethodGet,
		"/v1/payment_pages/"+url.PathEscape(checkout.ID),
		goPayElementsClientValues(snapshot, false),
		goPayPaymentPageReadFieldOrder,
		operation,
	)
	if err != nil {
		return stripeSnapshot{}, err
	}
	return goPaySnapshotFromPaymentPage(
		snapshot,
		response,
		checkout.ID,
		expectedPageID,
		initialChecksum,
		expectedHostedURL,
		expectedConfigID,
		operation,
	)
}

func goPaySnapshotFromPaymentPage(
	previous stripeSnapshot,
	response protocolJSONResponse,
	expectedCheckoutID,
	expectedPageID,
	initialChecksum,
	expectedHostedURL,
	expectedConfigID,
	stage string,
) (stripeSnapshot, error) {
	diagnostic := appendUpstreamResponseDiagnostic(
		previous.ResponseDiagnostic,
		response.ResponseDiagnostic,
	)
	fail := func(err error) (stripeSnapshot, error) {
		return stripeSnapshot{}, withUpstreamResponse(
			permanentWrap("Stripe GoPay "+stage+" contract", err),
			diagnostic,
		)
	}
	pageID, err := goPayRequiredTopLevelString(response.Payload, "id")
	if err != nil {
		return fail(err)
	}
	if !validCheckoutSessionIDWithPrefix(pageID, "ppage_") || pageID != expectedPageID {
		return fail(fmt.Errorf(
			"payment page changed: expected=%s actual=%s",
			expectedPageID,
			fallbackLabel(pageID, "<missing>"),
		))
	}
	sessionID, err := goPayRequiredTopLevelString(response.Payload, "session_id")
	if err != nil {
		return fail(err)
	}
	if sessionID != expectedCheckoutID {
		return fail(fmt.Errorf(
			"checkout session changed: expected=%s actual=%s",
			expectedCheckoutID,
			fallbackLabel(sessionID, "<missing>"),
		))
	}
	configID, err := goPayRequiredTopLevelString(response.Payload, "config_id")
	if err != nil {
		return fail(err)
	}
	if expectedConfigID != "" {
		if configID != expectedConfigID {
			return fail(fmt.Errorf(
				"checkout config changed: expected=%s actual=%s",
				expectedConfigID,
				configID,
			))
		}
	} else {
		previousConfigID := strings.TrimSpace(previous.Context.ConfigID)
		if previousConfigID == "" {
			return fail(errors.New("previous checkout config is missing"))
		}
		if configID == previousConfigID {
			return fail(errors.New("checkout config did not rotate after tax_region update"))
		}
	}
	checksum, err := goPayRequiredTopLevelString(response.Payload, "init_checksum")
	if err != nil {
		return fail(err)
	}
	if checksum != initialChecksum {
		return fail(errors.New("init_checksum changed after the initial Payment Page init"))
	}
	currency, err := goPayRequiredTopLevelString(response.Payload, "currency")
	if err != nil {
		return fail(err)
	}
	if strings.ToUpper(currency) != goPayCurrency {
		return fail(fmt.Errorf("currency changed to %s, want %s", currency, goPayCurrency))
	}
	methods := availablePaymentMethods(response.Payload)
	if !containsMethod(methods, "gopay") {
		return fail(fmt.Errorf(
			"gopay is unavailable (available=%s)",
			stripeMethodsSummary(methods),
		))
	}
	if country := goPayStripeGeocodingCountry(response.Payload); country != "" && country != goPayCountry {
		return fail(fmt.Errorf("geocoding country changed to %s, want %s", country, goPayCountry))
	}
	hostedURL, err := goPayRequiredTopLevelString(response.Payload, "stripe_hosted_url")
	if err != nil {
		return fail(err)
	}
	canonicalHostedURL, err := canonicalCSGoPayStripeHostedURL(hostedURL, expectedCheckoutID)
	if err != nil {
		return fail(err)
	}
	if canonicalHostedURL != expectedHostedURL {
		return fail(errors.New("stripe_hosted_url changed during the GoPay flow"))
	}
	amount := stripeAmount(response.Payload)
	if strings.TrimSpace(amount) == "" {
		return fail(errors.New("authoritative amount is missing"))
	}
	pricing := authoritativeStripePricing(response.Payload, "idr")
	current := previous.Context
	current.ConfigID = configID
	current.InitChecksum = initialChecksum
	current.Amount = amount
	current.Currency = "idr"
	current.ElementsLocale = goPayElementsLocale
	current.SavedPaymentMethodMode = "auto"
	return stripeSnapshot{
		Payload:            response.Payload,
		Context:            current,
		HostedURL:          canonicalHostedURL,
		Amount:             amount,
		Pricing:            pricing,
		Methods:            methods,
		PublishableKey:     previous.PublishableKey,
		ResponseDiagnostic: diagnostic,
	}, nil
}

func canonicalCSGoPayStripeHostedURL(value, checkoutID string) (string, error) {
	value = strings.TrimSpace(value)
	parsed, err := url.Parse(value)
	if err != nil || parsed == nil || !strings.EqualFold(parsed.Scheme, "https") ||
		parsed.User != nil || parsed.Port() != "" ||
		!strings.EqualFold(strings.TrimSuffix(parsed.Hostname(), "."), "checkout.stripe.com") {
		return "", errors.New("stripe_hosted_url is not an observed Stripe Checkout HTTPS URL")
	}
	wantedPath := "/c/pay/" + checkoutID
	if parsed.EscapedPath() != wantedPath {
		return "", fmt.Errorf(
			"stripe_hosted_url is not bound to checkout %s",
			checkoutID,
		)
	}
	return parsed.String(), nil
}

type goPayAddressPayload struct {
	Line1      string `json:"line1"`
	City       string `json:"city"`
	Country    string `json:"country"`
	PostalCode string `json:"postal_code"`
	State      string `json:"state"`
}

type goPayTaxesPayload struct {
	CheckoutSessionID string              `json:"checkout_session_id"`
	CheckoutEmail     string              `json:"checkout_email"`
	BillingCountry    string              `json:"billing_country"`
	BillingName       string              `json:"billing_name"`
	Currency          string              `json:"currency"`
	ProcessorEntity   string              `json:"processor_entity"`
	BillingAddress    goPayAddressPayload `json:"billing_address"`
}

type goPaySnapshotPayload struct {
	Snapshot struct {
		BillingAddress struct {
			Name    string              `json:"name"`
			Address goPayAddressPayload `json:"address"`
		} `json:"billing_address"`
	} `json:"snapshot"`
}

func goPayAddressFromBilling(billing billingDetails) goPayAddressPayload {
	return goPayAddressPayload{
		Line1:      strings.TrimSpace(billing.Line1),
		City:       strings.TrimSpace(billing.City),
		Country:    strings.ToUpper(strings.TrimSpace(billing.Country)),
		PostalCode: strings.TrimSpace(billing.PostalCode),
		State:      strings.TrimSpace(billing.State),
	}
}

func (executor *Executor) postCSGoPayTaxes(
	ctx context.Context,
	session *chatSession,
	checkout checkoutSession,
	billing billingDetails,
) (chatGPTResponse, error) {
	payload := goPayTaxesPayload{
		CheckoutSessionID: checkout.ID,
		CheckoutEmail:     strings.TrimSpace(billing.Email),
		BillingCountry:    goPayCountry,
		BillingName:       strings.TrimSpace(billing.Name),
		Currency:          "idr",
		ProcessorEntity:   goPayProcessorEntity,
		BillingAddress:    goPayAddressFromBilling(billing),
	}
	response, err := executor.postCSGoPayChatGPTJSON(
		ctx,
		session,
		checkout,
		goPayTaxesPath,
		payload,
		"ChatGPT GoPay taxes",
	)
	if err != nil {
		return chatGPTResponse{}, err
	}
	fail := func(err error) (chatGPTResponse, error) {
		return chatGPTResponse{}, withUpstreamResponse(
			permanentWrap("ChatGPT GoPay taxes contract", err),
			response.ResponseDiagnostic,
		)
	}
	usingAutomaticTax, ok := response.Payload["using_automatic_tax"].(bool)
	if !ok || !usingAutomaticTax {
		return fail(errors.New("using_automatic_tax is not true"))
	}
	rawCheckout, exists := response.Payload["checkout_session"]
	upstreamCheckout, ok := rawCheckout.(map[string]any)
	if !exists || !ok {
		return fail(errors.New("checkout_session is missing"))
	}
	checkoutID, parseErr := goPayRequiredTopLevelString(upstreamCheckout, "id")
	if parseErr != nil || checkoutID != checkout.ID {
		if parseErr == nil {
			parseErr = fmt.Errorf(
				"checkout session changed: expected=%s actual=%s",
				checkout.ID,
				fallbackLabel(checkoutID, "<missing>"),
			)
		}
		return fail(parseErr)
	}
	currency, parseErr := goPayRequiredTopLevelString(upstreamCheckout, "currency")
	if parseErr != nil || strings.ToUpper(currency) != goPayCurrency {
		if parseErr == nil {
			parseErr = fmt.Errorf("currency changed to %s, want %s", currency, goPayCurrency)
		}
		return fail(parseErr)
	}
	if !containsMethod(availablePaymentMethods(upstreamCheckout), "gopay") {
		return fail(errors.New("checkout_session no longer offers gopay"))
	}
	return response, nil
}

func (executor *Executor) postCSGoPaySnapshot(
	ctx context.Context,
	session *chatSession,
	checkout checkoutSession,
	billing billingDetails,
) (string, error) {
	payload := goPaySnapshotPayload{}
	payload.Snapshot.BillingAddress.Name = strings.TrimSpace(billing.Name)
	payload.Snapshot.BillingAddress.Address = goPayAddressFromBilling(billing)
	return executor.postCSGoPayChatGPTEmpty(
		ctx,
		session,
		checkout,
		goPayCheckoutSnapshot,
		payload,
		"ChatGPT GoPay billing snapshot",
	)
}

func goPayCheckoutReferer(executor *Executor, checkout checkoutSession) string {
	return executor.config.ChatGPTBaseURL + "/checkout/" +
		url.PathEscape(checkout.ProcessorEntity) + "/" +
		url.PathEscape(checkout.ID)
}

func (executor *Executor) postCSGoPayChatGPTJSON(
	ctx context.Context,
	session *chatSession,
	checkout checkoutSession,
	path string,
	payload any,
	operation string,
) (chatGPTResponse, error) {
	response, diagnostic, err := executor.doCSGoPayChatGPTRequest(
		ctx,
		session,
		checkout,
		path,
		payload,
		operation,
	)
	if err != nil {
		return chatGPTResponse{}, err
	}
	decoded, err := decodeJSONResponseDetails(response, operation)
	if err != nil {
		return chatGPTResponse{}, err
	}
	return chatGPTResponse{
		Payload:            decoded.Payload,
		RawJSON:            decoded.RawJSON,
		StatusCode:         response.StatusCode,
		ResponseDiagnostic: fallbackLabel(decoded.ResponseDiagnostic, diagnostic),
	}, nil
}

func (executor *Executor) postCSGoPayChatGPTEmpty(
	ctx context.Context,
	session *chatSession,
	checkout checkoutSession,
	path string,
	payload any,
	operation string,
) (string, error) {
	response, _, err := executor.doCSGoPayChatGPTRequest(
		ctx,
		session,
		checkout,
		path,
		payload,
		operation,
	)
	if err != nil {
		return "", err
	}
	if response == nil {
		return "", errors.New(operation + ": empty upstream response")
	}
	var responseBody []byte
	var readErr, closeErr error
	if response.Body != nil {
		responseBody, readErr = io.ReadAll(response.Body)
		closeErr = response.Body.Close()
	}
	diagnostic := httpResponseDiagnostic(response, operation, responseBody)
	failures := make([]error, 0, 3)
	if readErr != nil {
		failures = append(failures, fmt.Errorf(
			"%s: read upstream HTTP %d response: %w",
			operation,
			response.StatusCode,
			readErr,
		))
	}
	if closeErr != nil {
		failures = append(failures, fmt.Errorf(
			"%s: close upstream HTTP %d response: %w",
			operation,
			response.StatusCode,
			closeErr,
		))
	}
	if response.StatusCode != http.StatusNoContent || len(bytes.TrimSpace(responseBody)) != 0 {
		failures = append(failures,
			permanent(fmt.Sprintf(
				"%s: expected HTTP 204 with an empty body, got HTTP %d",
				operation,
				response.StatusCode,
			)),
		)
	}
	if len(failures) > 0 {
		return "", withUpstreamResponse(errors.Join(failures...), diagnostic)
	}
	return diagnostic, nil
}

func (executor *Executor) doCSGoPayChatGPTRequest(
	ctx context.Context,
	session *chatSession,
	checkout checkoutSession,
	path string,
	payload any,
	operation string,
) (*http.Response, string, error) {
	if session == nil || session.client == nil || session.executor == nil {
		return nil, "", errors.New(operation + ": ChatGPT session is missing")
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, "", fmt.Errorf("%s: encode request: %w", operation, err)
	}
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		executor.config.ChatGPTBaseURL+path,
		bytes.NewReader(body),
	)
	if err != nil {
		return nil, "", fmt.Errorf("%s: create request: %w", operation, err)
	}
	request.Header = session.browserHeaders()
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Referer", goPayCheckoutReferer(executor, checkout))
	request.Header.Set("x-openai-target-path", path)
	request.Header.Set("x-openai-target-route", path)
	request = alignChatGPTPaymentBrowserRequest(request, true)
	response, err := session.client.Do(request)
	if err != nil {
		requestErr := contextRequestError(ctx, operation, err)
		return nil, "", readHTTPResponseError(requestErr, response, operation)
	}
	session.syncDeviceIDFromCookies()
	return response, "", nil
}

type goPayInlineConfirmation struct {
	Snapshot           stripeSnapshot
	State              string
	Redirect           string
	ResponseDiagnostic string
}

// validateCSGoPayPromotionSnapshot keeps a create-time GoPay promotion bound
// to Stripe's authoritative amount-due field throughout the Payment Page
// state machine. A display amount of zero is not enough: promotionSynced also
// requires a parsed authoritative pricing snapshot and the selected method.
func validateCSGoPayPromotionSnapshot(
	snapshot stripeSnapshot,
	promoApplied bool,
	stage string,
) error {
	if !promoApplied {
		return nil
	}
	input := domain.CheckoutInput{
		UsePromo:      true,
		PromoCampaign: defaultPromo,
	}
	if promotionSynced(input, snapshot, "gopay") {
		return nil
	}
	dueToday := ""
	pricingSource := ""
	if pricing := authoritativeStripePricing(
		snapshot.Payload,
		snapshot.Context.Currency,
	); pricing != nil {
		dueToday = pricing.DueTodayMinor
		pricingSource = pricing.Source
	}
	return withUpstreamResponse(
		permanent(fmt.Sprintf(
			"GoPay create-time promotion lost authoritative zero due during %s: amount_minor=%s, due_today_minor=%s, source=%s",
			fallbackLabel(strings.TrimSpace(stage), "Payment Page update"),
			fallbackLabel(snapshot.Amount, "<missing>"),
			fallbackLabel(dueToday, "<missing>"),
			fallbackLabel(pricingSource, "<missing>"),
		)),
		snapshot.ResponseDiagnostic,
	)
}

// finishCSGoPay owns the shared Indonesia address and tax-region preparation.
// Production passes an aligned option to continue with the attachment-style
// pm_/approve/poll/provider state machine. The optionless continuation is kept
// temporarily for the captured pre-migration fixtures while those fixtures are
// being replaced; it is not selected by executeAttempt.
func (executor *Executor) finishCSGoPay(
	ctx context.Context,
	checkout checkoutSession,
	initial stripeSnapshot,
	stripeClient *http.Client,
	chatGPT *chatSession,
	accountEmail string,
	promoApplied bool,
	result domain.CheckoutResult,
	progress jobs.ProgressReporter,
	alignedOptions ...csGoPayAlignedFinishOptions,
) (*domain.CheckoutResult, error) {
	canonical, err := canonicalCSGoPayCheckout(checkout)
	if err != nil {
		return nil, err
	}
	if err := validateCSGoPayStripeInit(initial, canonical.ID); err != nil {
		return nil, err
	}
	if stripeClient == nil {
		return nil, errors.New("GoPay Stripe client is missing")
	}

	pageID, err := goPayRequiredTopLevelString(initial.Payload, "id")
	if err != nil {
		return nil, withUpstreamResponse(err, initial.ResponseDiagnostic)
	}
	initialConfigID := strings.TrimSpace(initial.Context.ConfigID)
	initialChecksum := strings.TrimSpace(initial.Context.InitChecksum)
	if initialConfigID == "" || initialChecksum == "" {
		return nil, withUpstreamResponse(
			permanent("GoPay initial Checkout config or checksum is missing"),
			initial.ResponseDiagnostic,
		)
	}
	hostedURL, err := canonicalCSGoPayStripeHostedURL(initial.HostedURL, canonical.ID)
	if err != nil {
		return nil, withUpstreamResponse(permanentWrap("GoPay initial Stripe hosted URL", err), initial.ResponseDiagnostic)
	}
	initial.HostedURL = hostedURL
	billing, err := executor.selectGoPayBilling(ctx, accountEmail)
	if err != nil {
		return nil, err
	}

	reportProgress(progress, 55, "正在初始化 GoPay Elements")
	elements, err := executor.fetchCSGoPayElementsSession(
		ctx,
		stripeClient,
		canonical,
		initial,
	)
	if err != nil {
		return nil, err
	}
	if elements.ConfigID == initialConfigID {
		return nil, withUpstreamResponse(
			permanent("GoPay Elements config did not separate from the initial Checkout config"),
			elements.ResponseDiagnostic,
		)
	}
	current := initial
	current.Context.ElementsSessionID = elements.SessionID
	current.Context.ElementsConfigID = elements.ConfigID
	current.Context.ElementsLocale = goPayElementsLocale
	current.Context.SavedPaymentMethodMode = "auto"
	current.ResponseDiagnostic = appendUpstreamResponseDiagnostic(
		current.ResponseDiagnostic,
		elements.ResponseDiagnostic,
	)
	reportSuccess(progress, 59, "GoPay Elements 初始化成功")

	// Stripe Elements emits four progressive address updates. Each returned
	// config becomes the input to the next update; none may rotate the page,
	// Checkout identity, checksum, market, or hosted URL.
	for fieldCount := 1; fieldCount <= 4; fieldCount++ {
		current, err = executor.updateCSGoPayTaxRegionFields(
			ctx,
			stripeClient,
			canonical,
			current,
			billing,
			fieldCount,
			pageID,
			initialChecksum,
			hostedURL,
		)
		if err != nil {
			return nil, err
		}
		if err := validateCSGoPayPromotionSnapshot(
			current,
			promoApplied,
			fmt.Sprintf("tax_region update (%d fields)", fieldCount),
		); err != nil {
			return nil, err
		}
	}
	preSnapshotConfigID := strings.TrimSpace(current.Context.ConfigID)
	if preSnapshotConfigID == "" {
		return nil, withUpstreamResponse(
			permanent("GoPay pre-snapshot Checkout config is missing"),
			current.ResponseDiagnostic,
		)
	}

	reportProgress(progress, 62, "正在同步 GoPay 税务与账单快照")
	taxesResponse, err := executor.postCSGoPayTaxes(ctx, chatGPT, canonical, billing)
	if err != nil {
		return nil, err
	}
	snapshotDiagnostic, err := executor.postCSGoPaySnapshot(ctx, chatGPT, canonical, billing)
	if err != nil {
		return nil, withUpstreamResponse(err, taxesResponse.ResponseDiagnostic)
	}
	current.ResponseDiagnostic = appendUpstreamResponseDiagnostic(
		current.ResponseDiagnostic,
		taxesResponse.ResponseDiagnostic,
	)
	current.ResponseDiagnostic = appendUpstreamResponseDiagnostic(
		current.ResponseDiagnostic,
		snapshotDiagnostic,
	)

	current, err = executor.refreshCSGoPayPaymentPage(
		ctx,
		stripeClient,
		canonical,
		current,
		pageID,
		initialChecksum,
		hostedURL,
		preSnapshotConfigID,
		"Stripe GoPay post-snapshot Payment Page refresh",
	)
	if err != nil {
		return nil, err
	}
	if err := validateCSGoPayPromotionSnapshot(
		current,
		promoApplied,
		"post-snapshot Payment Page refresh",
	); err != nil {
		return nil, err
	}
	current, err = executor.updateCSGoPayTaxRegion(
		ctx,
		stripeClient,
		canonical,
		current,
		billing,
		true,
		pageID,
		initialChecksum,
		hostedURL,
	)
	if err != nil {
		return nil, err
	}
	if err := validateCSGoPayPromotionSnapshot(
		current,
		promoApplied,
		"final tax_region update",
	); err != nil {
		return nil, err
	}
	finalConfigID := strings.TrimSpace(current.Context.ConfigID)
	if finalConfigID == "" || finalConfigID == initialConfigID ||
		finalConfigID == elements.ConfigID {
		return nil, withUpstreamResponse(
			permanent("GoPay final tax Checkout config is missing or crossed a captured config boundary"),
			current.ResponseDiagnostic,
		)
	}
	reportSuccess(progress, 68, "GoPay 税务与账单快照同步成功")
	if len(alignedOptions) > 0 {
		if len(alignedOptions) != 1 {
			return nil, errors.New("GoPay aligned finish received multiple option sets")
		}
		return executor.finishAlignedCSGoPayAfterTax(
			ctx,
			alignedOptions[0],
			canonical,
			current,
			billing,
			stripeClient,
			chatGPT,
			hostedURL,
			elements,
			promoApplied,
			result,
			progress,
		)
	}

	reportProgress(progress, 74, "正在确认 GoPay 支付")
	if err := validateCSGoPayPromotionSnapshot(
		current,
		promoApplied,
		"before inline confirm",
	); err != nil {
		return nil, err
	}
	confirmation, err := executor.confirmCSGoPayInline(
		ctx,
		stripeClient,
		canonical,
		current,
		billing,
		pageID,
		initialConfigID,
		initialChecksum,
		hostedURL,
	)
	if err != nil {
		return nil, err
	}
	if err := validateCSGoPayPromotionSnapshot(
		confirmation.Snapshot,
		promoApplied,
		"inline confirm response",
	); err != nil {
		return nil, err
	}
	approveSent := false
	approvalBlocked := false
	approvalDiagnostic := ""
	final := confirmation.Snapshot
	redirect := confirmation.Redirect
	resultState := confirmation.State

	if confirmation.State == "requires_approval" {
		if chatGPT == nil {
			return nil, withUpstreamResponse(
				errors.New("GoPay approval requires a ChatGPT session"),
				confirmation.ResponseDiagnostic,
			)
		}
		reportProgress(progress, 80, "正在提交 GoPay Checkout 审批")
		outcome, approveErr := executor.approveCheckoutWithPolicy(
			ctx,
			chatGPT,
			canonical,
			checkoutApprovalPolicy{
				allowPending:         true,
				prepareSentinelProof: true,
				requireSentinelProof: true,
			},
		)
		approveSent = true
		if approveErr != nil {
			return nil, withUpstreamResponse(approveErr, confirmation.ResponseDiagnostic)
		}
		approvalDiagnostic = outcome.ResponseDiagnostic
		if outcome.Result != "approved" && outcome.Result != "blocked" {
			failure := permanent(fmt.Sprintf(
				"GoPay approval returned unsupported result %s",
				fallbackLabel(outcome.Result, "<missing>"),
			))
			failure = withUpstreamResponse(failure, confirmation.ResponseDiagnostic)
			return nil, withUpstreamResponse(failure, outcome.ResponseDiagnostic)
		}
		approvalBlocked = outcome.Result == "blocked"

		// One approval consumes one fresh checkout_session_approval Sentinel
		// proof. The capture performs exactly one subsequent authoritative GET;
		// a second approve or a polling loop would cross the observed replay
		// boundary and is deliberately forbidden.
		final, err = executor.refreshCSGoPayPaymentPage(
			ctx,
			stripeClient,
			canonical,
			confirmation.Snapshot,
			pageID,
			initialChecksum,
			hostedURL,
			finalConfigID,
			"Stripe GoPay post-approval Payment Page",
		)
		if err != nil {
			err = withUpstreamResponse(err, confirmation.ResponseDiagnostic)
			return nil, withUpstreamResponse(err, outcome.ResponseDiagnostic)
		}
		if promoErr := validateCSGoPayPromotionSnapshot(
			final,
			promoApplied,
			"post-approval Payment Page",
		); promoErr != nil {
			promoErr = withUpstreamResponse(promoErr, confirmation.ResponseDiagnostic)
			return nil, withUpstreamResponse(promoErr, outcome.ResponseDiagnostic)
		}
		finalState, stateErr := goPaySubmissionState(final.Payload)
		if stateErr != nil {
			failure := withUpstreamResponse(stateErr, confirmation.ResponseDiagnostic)
			failure = withUpstreamResponse(failure, outcome.ResponseDiagnostic)
			return nil, withUpstreamResponse(failure, final.ResponseDiagnostic)
		}
		if finalState == "requires_approval" {
			failure := permanent("GoPay remained requires_approval after the single permitted approval")
			failure = withUpstreamResponse(failure, confirmation.ResponseDiagnostic)
			failure = withUpstreamResponse(failure, outcome.ResponseDiagnostic)
			return nil, withUpstreamResponse(failure, final.ResponseDiagnostic)
		}
		if finalState == "failed" {
			failure := stripeSubmissionFailure(final.Payload, "GoPay")
			failure = withUpstreamResponse(failure, confirmation.ResponseDiagnostic)
			failure = withUpstreamResponse(failure, outcome.ResponseDiagnostic)
			return nil, withUpstreamResponse(failure, final.ResponseDiagnostic)
		}
		if finalState != "complete" && finalState != "succeeded" {
			failure := permanent(fmt.Sprintf(
				"GoPay returned unsupported post-approval state %s",
				fallbackLabel(finalState, "<missing>"),
			))
			failure = withUpstreamResponse(failure, confirmation.ResponseDiagnostic)
			failure = withUpstreamResponse(failure, outcome.ResponseDiagnostic)
			return nil, withUpstreamResponse(failure, final.ResponseDiagnostic)
		}
		if approvalBlocked {
			failure := permanent(
				"GoPay approval was blocked; the post-approval Payment Page cannot be treated as success",
			)
			failure = withUpstreamResponse(failure, confirmation.ResponseDiagnostic)
			failure = withUpstreamResponse(failure, outcome.ResponseDiagnostic)
			return nil, withUpstreamResponse(failure, final.ResponseDiagnostic)
		}
		resultState = finalState
		redirect, _, err = goPayTypedRedirectURL(final.Payload)
		if err != nil {
			failure := withUpstreamResponse(err, confirmation.ResponseDiagnostic)
			failure = withUpstreamResponse(failure, outcome.ResponseDiagnostic)
			return nil, withUpstreamResponse(failure, final.ResponseDiagnostic)
		}
	} else if confirmation.State == "failed" {
		return nil, withUpstreamResponse(
			stripeSubmissionFailure(confirmation.Snapshot.Payload, "GoPay"),
			confirmation.ResponseDiagnostic,
		)
	} else if confirmation.State != "complete" && confirmation.State != "succeeded" {
		return nil, withUpstreamResponse(
			permanent(fmt.Sprintf(
				"GoPay returned unsupported confirmation state %s",
				fallbackLabel(confirmation.State, "<missing>"),
			)),
			confirmation.ResponseDiagnostic,
		)
	}

	if redirect == "" {
		failure := permanent("GoPay did not return a typed redirect_to_url action")
		failure = withUpstreamResponse(failure, confirmation.ResponseDiagnostic)
		failure = withUpstreamResponse(failure, approvalDiagnostic)
		return nil, withUpstreamResponse(failure, final.ResponseDiagnostic)
	}
	redirect, err = canonicalCSGoPayProviderRedirectURL(redirect)
	if err != nil {
		err = withUpstreamResponse(err, confirmation.ResponseDiagnostic)
		err = withUpstreamResponse(err, approvalDiagnostic)
		return nil, withUpstreamResponse(err, final.ResponseDiagnostic)
	}

	result.URL = redirect
	result.CheckoutURL = redirect
	result.ProviderRedirectURL = redirect
	// Keep the CS GoPay result boundary explicit even if a future caller passes
	// a partially populated result. Unrelated session, callback, QR, and custom
	// payment-method state must never leak into this redirect-only flow.
	result.GCashSessionID = ""
	result.GCashCallbackStatus = ""
	result.LongURL = ""
	result.PayPalRedirectURL = ""
	result.PayPalBAApproveURL = ""
	result.QRCodeData = ""
	result.QRCodeImagePNG = ""
	result.QRCodeImageSVG = ""
	result.CustomPaymentMethodTypeID = ""
	result.SelectedPaymentMethodType = ""
	result.PaymentMethodID = ""
	result.PaymentMethodType = "gopay"
	result.PaymentLinkType = "gopay_redirect"
	result.CheckoutLinkFamily = domain.CheckoutLinkFamilyCS
	result.CheckoutProvider = canonical.CheckoutProvider
	result.ProcessorEntity = canonical.ProcessorEntity
	result.PaymentMethodCountry = goPayCountry
	result.StripeAmount = final.Amount
	result.StripeAmountSource = stripeAmountSource(final)
	result.StripeHostedURL = hostedURL
	result.StripeElementsConfigID = elements.ConfigID
	result.SupportedPaymentMethods = append([]string(nil), final.Methods...)
	result.ConfirmStatus = resultState
	result.StripePaymentMethodCreated = boolPointer(false)
	result.ApproveSent = boolPointer(approveSent)
	result.RedirectFollowed = boolPointer(false)
	result.PromoApplied = promoApplied
	result.AmountMinor = final.Amount
	result.Amount = formatMinorAmount(final.Amount, canonical.Currency)
	result.Pricing = checkoutPricingSnapshot(final)
	// The provider URL is returned verbatim and is not fetched here. The HAR
	// supplies no trustworthy expiry for this typed redirect, so do not invent
	// a TTL or QR deadline.
	result.ExpiresAt = nil
	result.GeneratedAt = nil
	result.LinkGeneratedAt = nil
	result.LinkExpiresAt = nil
	result.QRCodeGeneratedAt = nil
	result.QRCodeExpiresAt = nil
	reportSuccess(progress, 96, "GoPay 支付链接已就绪")
	return &result, nil
}

func (executor *Executor) selectGoPayBilling(
	ctx context.Context,
	accountEmail string,
) (billingDetails, error) {
	billing := minimalBilling(goPayCountry, accountEmail)
	selected, err := gopayaddress.Random(nil)
	if err != nil {
		return billingDetails{}, permanentWrap("select GoPay synthetic billing address", err)
	}
	country := strings.ToUpper(strings.TrimSpace(selected.Country))
	if country != goPayCountry ||
		strings.TrimSpace(selected.Line1) == "" ||
		strings.TrimSpace(selected.City) == "" ||
		strings.TrimSpace(selected.State) == "" ||
		strings.TrimSpace(selected.PostalCode) == "" {
		return billingDetails{}, permanent(
			"select GoPay synthetic billing address: catalog returned an incomplete Indonesia address",
		)
	}
	billing.Name = "Pengguna Contoh"
	billing.Phone = "+6280000000000"
	billing.Country = country
	billing.Line1 = strings.TrimSpace(selected.Line1)
	billing.Line2 = strings.TrimSpace(selected.Line2)
	billing.City = strings.TrimSpace(selected.City)
	billing.State = strings.TrimSpace(selected.State)
	billing.PostalCode = strings.TrimSpace(selected.PostalCode)
	reportProtocolInfo(
		ctx,
		54,
		"账单地址",
		"GoPay 随机使用内置虚构地址记录: line1=%s, city=%s, state=%s, postal_code=%s, country=%s, catalog_id=%s",
		billing.Line1,
		billing.City,
		billing.State,
		billing.PostalCode,
		billing.Country,
		strings.TrimSpace(selected.ID),
	)
	return billing, nil
}

func (executor *Executor) confirmCSGoPayInline(
	ctx context.Context,
	client *http.Client,
	checkout checkoutSession,
	snapshot stripeSnapshot,
	billing billingDetails,
	pageID,
	initialConfigID,
	initialChecksum,
	hostedURL string,
) (goPayInlineConfirmation, error) {
	fail := func(err error, diagnostics ...string) (goPayInlineConfirmation, error) {
		failure := withUpstreamResponse(permanentWrap("Stripe GoPay inline confirm contract", err), snapshot.ResponseDiagnostic)
		for _, diagnostic := range diagnostics {
			failure = withUpstreamResponse(failure, diagnostic)
		}
		return goPayInlineConfirmation{}, failure
	}
	if !validGoPayCheckoutSessionID(checkout.ID) ||
		!validCheckoutSessionIDWithPrefix(pageID, "ppage_") ||
		strings.TrimSpace(initialConfigID) == "" ||
		strings.TrimSpace(snapshot.Context.ConfigID) == "" ||
		strings.TrimSpace(snapshot.Context.ElementsSessionID) == "" ||
		strings.TrimSpace(snapshot.Context.ElementsConfigID) == "" ||
		strings.TrimSpace(snapshot.Context.StripeJSID) == "" ||
		strings.TrimSpace(snapshot.PublishableKey) == "" ||
		strings.TrimSpace(snapshot.Amount) == "" ||
		strings.TrimSpace(snapshot.Context.Amount) == "" {
		return fail(errors.New("authoritative Stripe context is incomplete"))
	}
	if snapshot.Amount != snapshot.Context.Amount {
		return fail(errors.New("authoritative amount disagrees with Stripe context"))
	}
	if initialChecksum != snapshot.Context.InitChecksum {
		return fail(errors.New("initial checksum disagrees with Stripe context"))
	}
	guid, err := executor.newHostedStripeDeviceID()
	if err != nil {
		return goPayInlineConfirmation{}, fmt.Errorf("create GoPay Stripe guid: %w", err)
	}
	muid, err := executor.newHostedStripeDeviceID()
	if err != nil {
		return goPayInlineConfirmation{}, fmt.Errorf("create GoPay Stripe muid: %w", err)
	}
	sid, err := executor.newHostedStripeDeviceID()
	if err != nil {
		return goPayInlineConfirmation{}, fmt.Errorf("create GoPay Stripe sid: %w", err)
	}
	if !validHostedStripeDeviceID(guid) || !validHostedStripeDeviceID(muid) ||
		!validHostedStripeDeviceID(sid) {
		return fail(errors.New("generated Stripe device identifiers are invalid"))
	}
	jsChecksum, err := latestTest3StripeRuntimePayload(struct {
		ID string `json:"id"`
	}{ID: pageID})
	if err != nil {
		return goPayInlineConfirmation{}, fmt.Errorf("generate GoPay js_checksum: %w", err)
	}
	rvTimestamp, err := latestTest3StripeRuntimePayload(struct {
		RuntimeTimestamp string `json:"rvTs"`
		RuntimeRevision  string `json:"rv"`
		SchemaRevision   string `json:"sv"`
	}{
		RuntimeTimestamp: goPayStripeRuntimeTimestamp,
		RuntimeRevision:  goPayStripeRuntimeRevision,
		SchemaRevision:   goPayStripeSchemaRevision,
	})
	if err != nil {
		return goPayInlineConfirmation{}, fmt.Errorf("generate GoPay rv_timestamp: %w", err)
	}
	timeOnPage, err := executor.randomInt(stripeTimeOnPageMaximum - stripeTimeOnPageMinimum + 1)
	if err != nil {
		return goPayInlineConfirmation{}, fmt.Errorf("generate GoPay time_on_page: %w", err)
	}
	timeOnPage += stripeTimeOnPageMinimum
	returnURL, err := executor.csGoPayConfirmReturnURL(checkout, hostedURL)
	if err != nil {
		return fail(err)
	}
	paymentUserAgent := "stripe.js/" + goPayStripeRuntimeVersion +
		"; stripe-js-v3/" + goPayStripeRuntimeVersion +
		"; payment-element; deferred-intent"
	values := goPayElementsClientValues(snapshot, false)
	values.Set("guid", guid)
	values.Set("muid", muid)
	values.Set("sid", sid)
	values.Set("payment_method_data[billing_details][name]", billing.Name)
	values.Set("payment_method_data[billing_details][email]", billing.Email)
	values.Set("payment_method_data[billing_details][address][line1]", billing.Line1)
	values.Set("payment_method_data[billing_details][address][city]", billing.City)
	values.Set("payment_method_data[billing_details][address][postal_code]", billing.PostalCode)
	values.Set("payment_method_data[billing_details][address][state]", billing.State)
	values.Set("payment_method_data[billing_details][address][country]", billing.Country)
	values.Set("payment_method_data[type]", "gopay")
	values.Set("payment_method_data[payment_user_agent]", paymentUserAgent)
	values.Set("payment_method_data[referrer]", executor.config.ChatGPTBaseURL)
	values.Set("payment_method_data[time_on_page]", strconv.Itoa(timeOnPage))
	values.Set("payment_method_data[client_attribution_metadata][client_session_id]", snapshot.Context.StripeJSID)
	values.Set("payment_method_data[client_attribution_metadata][checkout_session_id]", checkout.ID)
	values.Set("payment_method_data[client_attribution_metadata][merchant_integration_source]", "elements")
	values.Set("payment_method_data[client_attribution_metadata][merchant_integration_subtype]", "payment-element")
	values.Set("payment_method_data[client_attribution_metadata][merchant_integration_version]", "2021")
	values.Set("payment_method_data[client_attribution_metadata][payment_intent_creation_flow]", "deferred")
	values.Set("payment_method_data[client_attribution_metadata][payment_method_selection_flow]", "merchant_specified")
	values.Set("payment_method_data[client_attribution_metadata][elements_session_id]", snapshot.Context.ElementsSessionID)
	values.Set("payment_method_data[client_attribution_metadata][elements_session_config_id]", snapshot.Context.ElementsConfigID)
	values.Set("payment_method_data[client_attribution_metadata][checkout_config_id]", initialConfigID)
	for index, element := range []string{"expressCheckout", "payment", "address"} {
		values.Set(
			fmt.Sprintf("payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][%d]", index),
			element,
		)
		values.Set(
			fmt.Sprintf("client_attribution_metadata[merchant_integration_additional_elements][%d]", index),
			element,
		)
	}
	values.Set("init_checksum", initialChecksum)
	values.Set("version", goPayStripeRuntimeVersion)
	values.Set("expected_amount", snapshot.Amount)
	values.Set("js_checksum", jsChecksum)
	values.Set("rv_timestamp", rvTimestamp)
	values.Set("expected_payment_method_type", "gopay")
	values.Set("return_url", returnURL)
	values.Set("client_attribution_metadata[client_session_id]", snapshot.Context.StripeJSID)
	values.Set("client_attribution_metadata[checkout_session_id]", checkout.ID)
	values.Set("client_attribution_metadata[merchant_integration_source]", "checkout")
	values.Set("client_attribution_metadata[merchant_integration_version]", "custom")
	values.Set("client_attribution_metadata[merchant_integration_subtype]", "payment-element")
	values.Set("client_attribution_metadata[payment_intent_creation_flow]", "deferred")
	values.Set("client_attribution_metadata[payment_method_selection_flow]", "merchant_specified")
	values.Set("client_attribution_metadata[elements_session_id]", snapshot.Context.ElementsSessionID)
	values.Set("client_attribution_metadata[elements_session_config_id]", snapshot.Context.ElementsConfigID)
	values.Set("client_attribution_metadata[checkout_config_id]", snapshot.Context.ConfigID)
	values.Set("link_brand", "link")

	response, err := executor.doGoPayStripeFormResponse(
		ctx,
		client,
		http.MethodPost,
		"/v1/payment_pages/"+url.PathEscape(checkout.ID)+"/confirm",
		values,
		goPayConfirmFieldOrder,
		"Stripe GoPay inline confirm",
	)
	if err != nil {
		return goPayInlineConfirmation{}, err
	}
	confirmed, err := goPaySnapshotFromPaymentPage(
		snapshot,
		response,
		checkout.ID,
		pageID,
		initialChecksum,
		hostedURL,
		snapshot.Context.ConfigID,
		"inline confirm",
	)
	if err != nil {
		return goPayInlineConfirmation{}, err
	}
	state, err := goPaySubmissionState(response.Payload)
	if err != nil {
		return fail(err, response.ResponseDiagnostic)
	}
	redirect, _, err := goPayTypedRedirectURL(response.Payload)
	if err != nil {
		return fail(err, response.ResponseDiagnostic)
	}
	return goPayInlineConfirmation{
		Snapshot:           confirmed,
		State:              state,
		Redirect:           redirect,
		ResponseDiagnostic: response.ResponseDiagnostic,
	}, nil
}

func (executor *Executor) csGoPayConfirmReturnURL(
	checkout checkoutSession,
	hostedURL string,
) (string, error) {
	canonical, err := canonicalCSGoPayStripeHostedURL(hostedURL, checkout.ID)
	if err != nil {
		return "", err
	}
	parsed, err := url.Parse(canonical)
	if err != nil || parsed == nil {
		return "", errors.New("parse GoPay Stripe hosted URL")
	}
	parsed.RawQuery = "returned_from_redirect=true&ui_mode=custom&return_url=" +
		url.QueryEscape(executor.checkoutVerifyURL(checkout))
	return parsed.String(), nil
}

func goPaySubmissionState(payload map[string]any) (string, error) {
	rawAttempt, exists := payload["submission_attempt"]
	attempt, ok := rawAttempt.(map[string]any)
	if !exists || !ok {
		return "", errors.New("top-level submission_attempt is missing or invalid")
	}
	rawState, exists := attempt["state"]
	state, ok := rawState.(string)
	state = strings.ToLower(strings.TrimSpace(state))
	if !exists || !ok || state == "" || strings.ContainsAny(state, "\x00\r\n") {
		return "", errors.New("submission_attempt.state is missing or invalid")
	}
	return state, nil
}

func goPayTypedRedirectURL(payload map[string]any) (string, bool, error) {
	rawAction, exists := payload["next_action"]
	if !exists || rawAction == nil {
		return "", false, nil
	}
	action, ok := rawAction.(map[string]any)
	if !ok {
		return "", false, permanent("GoPay next_action is invalid")
	}
	rawType, present := action["type"]
	actionType, ok := rawType.(string)
	if !present || !ok || strings.TrimSpace(actionType) != "redirect_to_url" {
		return "", false, permanent("GoPay next_action is not typed redirect_to_url")
	}
	rawRedirect, present := action["redirect_to_url"]
	redirectObject, ok := rawRedirect.(map[string]any)
	if !present || !ok {
		return "", false, permanent("GoPay redirect_to_url object is missing or invalid")
	}
	rawURL, present := redirectObject["url"]
	value, ok := rawURL.(string)
	if !present || !ok || value == "" || value != strings.TrimSpace(value) {
		return "", false, permanent("GoPay redirect_to_url.url is missing or invalid")
	}
	canonical, err := canonicalCSGoPayProviderRedirectURL(value)
	if err != nil {
		return "", false, err
	}
	return canonical, true, nil
}

func canonicalCSGoPayProviderRedirectURL(value string) (string, error) {
	if value == "" || value != strings.TrimSpace(value) || strings.ContainsAny(value, "\x00\r\n") {
		return "", permanent("GoPay provider redirect URL is missing or invalid")
	}
	parsed, err := url.Parse(value)
	if err != nil || parsed == nil || !parsed.IsAbs() || parsed.Opaque != "" ||
		!strings.EqualFold(parsed.Scheme, "https") || parsed.Host == "" ||
		parsed.User != nil || parsed.Port() != "" {
		return "", permanent("GoPay provider redirect must be absolute HTTPS without credentials or a custom port")
	}
	return value, nil
}
