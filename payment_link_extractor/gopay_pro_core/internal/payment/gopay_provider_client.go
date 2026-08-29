package payment

import (
	"errors"
	"fmt"
	"net/http"
	"strings"

	"go-chatgpt/internal/browserhttp"
)

// newGoPayProviderRedirectClient creates the provider-navigation boundary for
// GoPay. The selected proxy and browser profile remain stable, while omitting
// a source Jar ensures ChatGPT and Stripe cookies never cross into the provider
// redirect chain.
func (executor *Executor) newGoPayProviderRedirectClient(
	proxyURL string,
	profile browserhttp.Profile,
) (*http.Client, error) {
	if executor == nil {
		return nil, errors.New("GoPay provider redirect executor is missing")
	}
	resolvedProfile, err := attachmentCSGoPayProfile(profile)
	if err != nil {
		return nil, fmt.Errorf("prepare GoPay provider redirect profile: %w", err)
	}
	client, err := executor.newBrowserHTTPClient(
		strings.TrimSpace(proxyURL),
		resolvedProfile,
	)
	if err != nil {
		return nil, fmt.Errorf("prepare GoPay provider redirect client: %w", err)
	}
	client.CheckRedirect = func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}
	return client, nil
}
