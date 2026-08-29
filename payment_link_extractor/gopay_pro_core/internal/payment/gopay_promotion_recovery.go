package payment

import (
	"context"
	"fmt"
	"strings"

	"go-chatgpt/internal/accounts"
	"go-chatgpt/internal/domain"
	"go-chatgpt/internal/jobs"
)

const (
	goPayPromotionDetectionMaxAttempts = 10
	goPayPromotionIneligibleErrorCode  = "gopay_promotion_ineligible"
)

type goPayPromotionIneligibleError struct {
	Detections int
}

func (err *goPayPromotionIneligibleError) Error() string {
	return fmt.Sprintf(
		"error_code=%s: GoPay promotion eligibility was absent in %d consecutive detection attempts",
		goPayPromotionIneligibleErrorCode,
		err.Detections,
	)
}

// probeGoPayPromotionProxy resolves the account's current GoPay trial before a
// real Checkout is created. Newly used probe routes come from the same entry
// pool as Checkout and immediately enter the shared cooldown. Once this task
// has reserved every currently available route, it may reuse its own reserved
// routes without renewing their cooldown instead of waiting six minutes between
// accounts/check calls. On success the caller must reuse the returned route for
// that Checkout. Only ten authoritative accounts/check responses without the
// requested trial are classified as ineligible; transport, HTTP, and decoding
// failures remain their original errors and never count as an absent promotion.
func (executor *Executor) probeGoPayPromotionProxy(
	ctx context.Context,
	input domain.CheckoutInput,
	fingerprint paymentFingerprint,
	progress jobs.ProgressReporter,
) (string, error) {
	if len(input.EntryProxies) == 0 {
		return "", permanent("GoPay promotion probe requires creation/payment proxy routes")
	}
	entryRoutes := uniqueGoPayProxyRoutes(input.EntryProxies)
	if len(entryRoutes) == 0 {
		return "", permanent("GoPay promotion probe requires creation/payment proxy routes")
	}

	consecutiveAbsent := 0
	reservedRoutes := make([]string, 0, len(entryRoutes))
	reservedRouteSet := make(map[string]struct{}, len(entryRoutes))
	reuseIndex := 0
	for consecutiveAbsent < goPayPromotionDetectionMaxAttempts {
		unreservedRoutes := make([]string, 0, len(entryRoutes)-len(reservedRoutes))
		for _, route := range entryRoutes {
			if _, reserved := reservedRouteSet[route]; reserved {
				continue
			}
			unreservedRoutes = append(unreservedRoutes, route)
		}

		var (
			detectionProxy string
			reusedReserved bool
		)
		for detectionProxy == "" {
			if len(unreservedRoutes) > 0 {
				availableRoute, retryAfter, err := executor.tryAcquireRandomGoPayProxy(
					ctx,
					unreservedRoutes,
					"promotion probe",
				)
				if err != nil {
					return "", fmt.Errorf("acquire GoPay promotion probe proxy: %w", err)
				}
				if availableRoute != "" {
					detectionProxy = availableRoute
					reservedRoutes = append(reservedRoutes, availableRoute)
					reservedRouteSet[availableRoute] = struct{}{}
					break
				}
				if len(reservedRoutes) == 0 {
					if err := executor.waitForGoPayProxy(ctx, retryAfter); err != nil {
						return "", fmt.Errorf("wait for GoPay promotion probe proxy: %w", err)
					}
					continue
				}
			}

			if len(reservedRoutes) == 0 {
				return "", fmt.Errorf("GoPay promotion probe has no reserved proxy route")
			}
			detectionProxy = reservedRoutes[reuseIndex%len(reservedRoutes)]
			reuseIndex++
			reusedReserved = true
		}
		reportProgress(
			progress,
			5,
			fmt.Sprintf(
				"正在使用创建/支付代理核验试用优惠（%d/%d）",
				consecutiveAbsent+1,
				goPayPromotionDetectionMaxAttempts,
			),
		)
		if reusedReserved {
			reportProtocolInfo(
				ctx,
				5,
				"promo",
				"GoPay 创建/支付代理池其余路线均在共享冷却；复用本任务已领取路线继续试用检测且不续冷却: route=%s",
				maskedProxyRoute(detectionProxy),
			)
		} else {
			reportProtocolInfo(
				ctx,
				5,
				"promo",
				"GoPay 创建/支付代理已随机领取用于试用检测并进入 6 分钟冷却: route=%s",
				maskedProxyRoute(detectionProxy),
			)
		}
		probeCtx := executor.withInspectedPaymentAttemptProxyRoutes(
			ctx,
			[]paymentAttemptProxyRoute{{
				phase:    "promotion_detection",
				proxyURL: detectionProxy,
			}},
		)

		probeFingerprint, err := newGoPayPhaseFingerprint(fingerprint)
		if err != nil {
			return "", err
		}
		probeSession, err := executor.newChatSessionWithFingerprint(
			input.AccessToken,
			detectionProxy,
			probeFingerprint,
		)
		if err != nil {
			return "", fmt.Errorf("prepare GoPay promotion probe session: %w", err)
		}
		probeSession.accountID = strings.TrimSpace(input.ChatGPTAccountID)
		probeSession.configureCheckoutMarket(
			string(domain.PaymentMethodGoPay),
			input.Country,
			input.Currency,
		)

		campaign, detectionErr := executor.resolveAccountPlusPromotion(
			probeCtx,
			probeSession,
			input,
			"GoPay promotion detection",
		)
		closeHTTPClient(probeSession.client)
		if detectionErr != nil {
			return "", detectionErr
		}
		if !goPayCampaignEligible(campaign, input.PromoCampaign) {
			consecutiveAbsent++
			reportWarning(
				progress,
				5,
				fmt.Sprintf(
					"本次检测没有试用优惠（%d/%d）",
					consecutiveAbsent,
					goPayPromotionDetectionMaxAttempts,
				),
			)
			continue
		}

		reportSuccess(progress, 6, "检测到试用优惠，将复用当前代理创建全新 Checkout")
		reportProtocolSuccess(
			ctx,
			6,
			"promo",
			"GoPay 试用资格检测成功；正式 Checkout 将复用当前代理: route=%s",
			maskedProxyRoute(detectionProxy),
		)
		return detectionProxy, nil
	}

	return "", permanentError{err: &goPayPromotionIneligibleError{
		Detections: consecutiveAbsent,
	}}
}

func goPayCampaignEligible(
	campaign accounts.PlusPromoCampaign,
	expectedCampaign string,
) bool {
	campaignID := strings.TrimSpace(campaign.ID)
	if campaignID == "" {
		campaignID = strings.TrimSpace(campaign.CampaignID)
	}
	expectedCampaign = strings.TrimSpace(expectedCampaign)
	if campaignID == "" || expectedCampaign == "" ||
		!strings.EqualFold(campaignID, expectedCampaign) {
		return false
	}

	discountPercent := campaign.DiscountPercent
	if discountPercent == 0 && campaign.DiscountPercentage != 0 {
		discountPercent = campaign.DiscountPercentage
	}
	// accounts/check uses -1 when the campaign is authoritative but omits its
	// percentage. The configured GoPay trial is otherwise expected to be fully
	// discounted; a different or partial Plus activity must not confirm this
	// task's requested one-month-free campaign.
	return discountPercent == -1 || discountPercent == 100
}
