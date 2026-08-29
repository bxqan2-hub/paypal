package payment

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"go-chatgpt/internal/browserhttp"
	"go-chatgpt/internal/domain"
	"go-chatgpt/internal/jobs"
)

type csGoPayAlignedFinishOptions struct {
	Input   domain.CheckoutInput
	Profile browserhttp.Profile
}

// finishAlignedCSGoPayAfterTax continues after the synthetic billing-address
// and progressive tax-region sequence. Every subsequent request is implemented
// in Go and follows the attachment protocol without invoking the attachment at
// runtime.
func (executor *Executor) finishAlignedCSGoPayAfterTax(
	ctx context.Context,
	options csGoPayAlignedFinishOptions,
	checkout checkoutSession,
	prepared stripeSnapshot,
	billing billingDetails,
	stripeClient *http.Client,
	chatGPT *chatSession,
	hostedURL string,
	elements goPayElementsSession,
	promoApplied bool,
	result domain.CheckoutResult,
	progress jobs.ProgressReporter,
) (*domain.CheckoutResult, error) {
	if stripeClient == nil {
		return nil, errors.New("GoPay aligned Stripe client is missing")
	}
	if chatGPT == nil || chatGPT.client == nil {
		return nil, errors.New("GoPay aligned ChatGPT session is missing")
	}
	profile, err := attachmentCSGoPayProfile(options.Profile)
	if err != nil {
		return nil, fmt.Errorf("prepare GoPay aligned browser profile: %w", err)
	}

	priorLinkBrand := attachmentCSGoPayLinkBrand(prepared.Payload)
	reportProgress(progress, 70, "正在刷新 GoPay 最终 Stripe 状态")
	final, err := executor.stripeInitAttachmentCSGoPay(
		ctx,
		stripeClient,
		profile,
		checkout,
		prepared,
	)
	if err != nil {
		return nil, withUpstreamResponse(err, prepared.ResponseDiagnostic)
	}
	final = withAttachmentCSGoPayLinkBrandFallback(final, priorLinkBrand)
	if err := validateCSGoPayPromotionSnapshot(
		final,
		promoApplied,
		"final Stripe init after taxes",
	); err != nil {
		return nil, err
	}
	reportSuccess(progress, 72, "GoPay 最终 Stripe 状态刷新成功")

	reportProgress(progress, 74, "正在准备 GoPay pm_ 支付方式")
	preConfirm, err := executor.preConfirmAttachmentCSGoPay(
		ctx,
		stripeClient,
		profile,
		checkout,
		final,
	)
	if err != nil {
		return nil, withUpstreamResponse(err, final.ResponseDiagnostic)
	}
	flowDiagnostic := appendUpstreamResponseDiagnostic(
		final.ResponseDiagnostic,
		preConfirm.ResponseDiagnostic,
	)
	ids, err := executor.newAttachmentCSGoPayBrowserIDs()
	if err != nil {
		return nil, withUpstreamResponse(err, flowDiagnostic)
	}
	paymentMethod, err := executor.createAttachmentCSGoPayPaymentMethod(
		ctx,
		stripeClient,
		profile,
		checkout,
		final,
		ids,
		billing,
	)
	if err != nil {
		return nil, withUpstreamResponse(err, flowDiagnostic)
	}
	flowDiagnostic = appendUpstreamResponseDiagnostic(
		flowDiagnostic,
		paymentMethod.Response.ResponseDiagnostic,
	)
	reportSuccess(progress, 78, "GoPay pm_ 支付方式创建成功")

	reportProgress(progress, 80, "正在确认 GoPay 支付")
	confirmation, err := executor.confirmAttachmentCSGoPayWithPaymentMethod(
		ctx,
		stripeClient,
		profile,
		checkout,
		final,
		ids,
		paymentMethod.ID,
	)
	if err != nil {
		return nil, withUpstreamResponse(err, flowDiagnostic)
	}
	flowDiagnostic = appendUpstreamResponseDiagnostic(
		flowDiagnostic,
		confirmation.Response.ResponseDiagnostic,
	)
	current := withAttachmentCSGoPayLinkBrandFallback(confirmation.Snapshot, priorLinkBrand)
	redirect := strings.TrimSpace(confirmation.Redirect)
	approveSent := false
	providerProxy := strings.TrimSpace(chatGPT.proxyURL)
	providerProfile := profile

	var approval goPayApprovalResult
	if redirect == "" {
		reportProgress(progress, 84, "正在提交 GoPay Checkout 审批")
		approval, err = executor.approveGoPayCheckoutWithProxyRotation(
			ctx,
			options.Input,
			checkout,
			chatGPT,
		)
		approveSent = approval.Sent
		defer approval.Close()
		if err != nil {
			return nil, withUpstreamResponse(err, flowDiagnostic)
		}
		flowDiagnostic = appendUpstreamResponseDiagnostic(
			flowDiagnostic,
			approval.Outcome.ResponseDiagnostic,
		)
		redirect = strings.TrimSpace(approval.Outcome.Redirect)
		providerProxy = strings.TrimSpace(approval.ProxyURL)
		providerProfile = approval.Profile
	}

	if redirect == "" {
		reportProgress(progress, 88, "正在轮询 GoPay provider 跳转")
		poll, pollErr := executor.pollAttachmentCSGoPayPaymentPage(
			ctx,
			stripeClient,
			profile,
			checkout,
			current,
			nil,
		)
		if pollErr != nil {
			return nil, withUpstreamResponse(pollErr, flowDiagnostic)
		}
		flowDiagnostic = appendUpstreamResponseDiagnostic(
			flowDiagnostic,
			poll.Response.ResponseDiagnostic,
		)
		redirect = strings.TrimSpace(poll.Redirect)
		if poll.Response.Payload != nil {
			current = withAttachmentCSGoPayLinkBrandFallback(
				attachmentCSGoPaySnapshotFromResponse(current, poll.Response),
				priorLinkBrand,
			)
		}
	}
	if redirect == "" {
		return nil, withUpstreamResponse(
			errors.New("GoPay redirect link was not returned after confirm, approve, and poll"),
			flowDiagnostic,
		)
	}

	reportProgress(progress, 93, "正在解析 GoPay provider 最终链接")
	providerClient, err := executor.newGoPayProviderRedirectClient(
		providerProxy,
		providerProfile,
	)
	if err != nil {
		return nil, withUpstreamResponse(err, flowDiagnostic)
	}
	defer closeHTTPClient(providerClient)
	providerURL, err := resolveAttachmentCSGoPayProviderRedirect(
		ctx,
		providerClient,
		providerProfile,
		redirect,
		nil,
	)
	if err != nil {
		return nil, withUpstreamResponse(err, flowDiagnostic)
	}

	result.URL = providerURL
	result.PaymentMethod = domain.PaymentMethodGoPay
	result.Plan = domain.PlanPlus
	result.LinkType = domain.PaymentMethodGoPay
	result.CheckoutSessionID = checkout.ID
	result.CheckoutURL = providerURL
	result.ProviderRedirectURL = providerURL
	result.CheckoutLinkFamily = domain.CheckoutLinkFamilyCS
	result.PaymentStatus = PaymentStatusLinkReady
	result.PaymentLinkType = "gopay_redirect"
	result.PaymentMethodID = paymentMethod.ID
	result.PaymentMethodType = "gopay"
	result.CheckoutProvider = checkout.CheckoutProvider
	result.ProcessorEntity = checkout.ProcessorEntity
	result.PaymentMethodCountry = goPayCountry
	result.Country = goPayCountry
	result.Currency = goPayCurrency
	result.PromoRequested = options.Input.UsePromo
	result.PromoApplied = promoApplied
	result.PromoCampaign = options.Input.PromoCampaign
	result.PaymentCollection = domain.PaymentCollectionRequired
	result.StripeAmount = current.Amount
	result.StripeAmountSource = stripeAmountSource(current)
	result.StripeHostedURL = hostedURL
	result.StripeElementsConfigID = elements.ConfigID
	result.SupportedPaymentMethods = append([]string(nil), current.Methods...)
	result.ConfirmStatus = firstNonEmptyAttachmentCSGoPayValue(
		attachmentCSGoPaySubmissionState(current.Payload),
		strings.ToLower(strings.TrimSpace(mapString(current.Payload, "status"))),
		strings.ToLower(strings.TrimSpace(mapString(current.Payload, "payment_status"))),
	)
	result.StripePaymentMethodCreated = boolPointer(true)
	result.ApproveSent = boolPointer(approveSent)
	result.RedirectFollowed = boolPointer(true)
	result.AmountMinor = current.Amount
	result.Amount = formatMinorAmount(current.Amount, goPayCurrency)
	result.Pricing = checkoutPricingSnapshot(current)
	result.RawStatus = result.ConfirmStatus

	// The CS result is redirect-only. Clear unrelated payment fields so a reused
	// base result cannot leak stale state.
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
	result.ExpiresAt = nil
	result.GeneratedAt = nil
	result.LinkGeneratedAt = nil
	result.LinkExpiresAt = nil
	result.QRCodeGeneratedAt = nil
	result.QRCodeExpiresAt = nil
	reportSuccess(progress, 96, "GoPay 支付链接已就绪")
	return &result, nil
}
