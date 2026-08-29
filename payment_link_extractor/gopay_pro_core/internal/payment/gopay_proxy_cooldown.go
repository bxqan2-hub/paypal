package payment

import (
	"context"
	cryptorand "crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math/big"
	"strings"
	"time"
)

var errGoPayProxyPoolEmpty = errors.New("GoPay proxy pool has no available routes")

// acquireRandomGoPayProxy atomically chooses one currently available route and
// immediately starts its cooldown. When every candidate is cooling it waits for
// the earliest route, preserving the original blocking scheduler behavior.
func (executor *Executor) acquireRandomGoPayProxy(
	ctx context.Context,
	candidates []string,
	purpose string,
) (string, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	for {
		route, retryAfter, err := executor.tryAcquireRandomGoPayProxy(
			ctx,
			candidates,
			purpose,
		)
		if err != nil {
			return "", err
		}
		if route != "" {
			return route, nil
		}
		if retryAfter <= 0 {
			return "", fmt.Errorf(
				"reserve %s GoPay proxy cooldown returned no retry delay",
				purpose,
			)
		}
		if err := executor.waitForGoPayProxy(ctx, retryAfter); err != nil {
			return "", err
		}
	}
}

// tryAcquireRandomGoPayProxy makes one non-blocking reservation attempt. An
// empty route with a positive retryAfter means every candidate is currently
// cooling. The registry belongs to the shared payment Executor (or its durable
// cooldown store), so a successful reservation remains exclusive across other
// checkout workers for the configured cooldown window.
func (executor *Executor) tryAcquireRandomGoPayProxy(
	ctx context.Context,
	candidates []string,
	purpose string,
) (string, time.Duration, error) {
	if executor == nil {
		return "", 0, errors.New("GoPay proxy scheduler is missing")
	}
	routes := uniqueGoPayProxyRoutes(candidates)
	if len(routes) == 0 {
		return "", 0, fmt.Errorf(
			"%w: %s",
			errGoPayProxyPoolEmpty,
			strings.TrimSpace(purpose),
		)
	}
	if ctx == nil {
		ctx = context.Background()
	}
	if err := ctx.Err(); err != nil {
		return "", 0, err
	}

	if executor.config.GoPayProxyCooldownStore != nil {
		shuffled, err := executor.shuffleGoPayProxyRoutes(routes)
		if err != nil {
			return "", 0, fmt.Errorf("randomly order %s GoPay proxies: %w", purpose, err)
		}
		keys := make([]string, len(shuffled))
		for index, route := range shuffled {
			keys[index] = goPayProxyCooldownKey(route)
		}
		selected, retryAfter, err := executor.config.GoPayProxyCooldownStore.
			AcquireGoPayProxyCooldown(
				ctx,
				keys,
				executor.config.GoPayProxyCooldown,
			)
		if err != nil {
			return "", 0, fmt.Errorf("reserve %s GoPay proxy cooldown: %w", purpose, err)
		}
		if selected >= 0 {
			if selected >= len(shuffled) {
				return "", 0, fmt.Errorf(
					"reserve %s GoPay proxy cooldown returned invalid index %d",
					purpose,
					selected,
				)
			}
			return shuffled[selected], 0, nil
		}
		if selected != -1 {
			return "", 0, fmt.Errorf(
				"reserve %s GoPay proxy cooldown returned invalid index %d",
				purpose,
				selected,
			)
		}
		if retryAfter <= 0 {
			return "", 0, fmt.Errorf(
				"reserve %s GoPay proxy cooldown returned no retry delay",
				purpose,
			)
		}
		return "", retryAfter, nil
	}

	now := executor.config.Now().UTC()
	executor.goPayProxyMu.Lock()
	available := make([]string, 0, len(routes))
	var earliest time.Time
	for _, route := range routes {
		cooldownUntil := executor.goPayProxyCooldowns[route]
		if cooldownUntil.IsZero() || !now.Before(cooldownUntil) {
			delete(executor.goPayProxyCooldowns, route)
			available = append(available, route)
			continue
		}
		if earliest.IsZero() || cooldownUntil.Before(earliest) {
			earliest = cooldownUntil
		}
	}
	if len(available) > 0 {
		index, err := executor.randomGoPayProxyIndex(len(available))
		if err != nil {
			executor.goPayProxyMu.Unlock()
			return "", 0, fmt.Errorf("randomly acquire %s GoPay proxy: %w", purpose, err)
		}
		selected := available[index]
		executor.goPayProxyCooldowns[selected] = now.Add(
			executor.config.GoPayProxyCooldown,
		)
		executor.goPayProxyMu.Unlock()
		return selected, 0, nil
	}
	executor.goPayProxyMu.Unlock()

	retryAfter := earliest.Sub(now)
	if retryAfter <= 0 {
		return "", 0, fmt.Errorf(
			"reserve %s GoPay proxy cooldown returned no retry delay",
			purpose,
		)
	}
	return "", retryAfter, nil
}

func (executor *Executor) waitForGoPayProxy(ctx context.Context, wait time.Duration) error {
	if wait <= 0 {
		return nil
	}
	waiter := executor.retryWait
	if waiter == nil {
		waiter = waitContext
	}
	return waiter(ctx, wait)
}

func (executor *Executor) shuffleGoPayProxyRoutes(routes []string) ([]string, error) {
	shuffled := append([]string(nil), routes...)
	for upper := len(shuffled); upper > 1; upper-- {
		index, err := executor.randomGoPayProxyIndex(upper)
		if err != nil {
			return nil, err
		}
		shuffled[upper-1], shuffled[index] = shuffled[index], shuffled[upper-1]
	}
	return shuffled, nil
}

func goPayProxyCooldownKey(route string) string {
	digest := sha256.Sum256([]byte(strings.TrimSpace(route)))
	return hex.EncodeToString(digest[:])
}

func (executor *Executor) randomGoPayProxyIndex(length int) (int, error) {
	if length <= 0 {
		return 0, errGoPayProxyPoolEmpty
	}
	executor.goPayProxyRandomMu.Lock()
	defer executor.goPayProxyRandomMu.Unlock()
	value, err := cryptorand.Int(
		executor.config.GoPayProxyRandom,
		big.NewInt(int64(length)),
	)
	if err != nil {
		return 0, err
	}
	return int(value.Int64()), nil
}

func uniqueGoPayProxyRoutes(candidates []string) []string {
	routes := make([]string, 0, len(candidates))
	seen := make(map[string]struct{}, len(candidates))
	for _, candidate := range candidates {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			continue
		}
		if _, exists := seen[candidate]; exists {
			continue
		}
		seen[candidate] = struct{}{}
		routes = append(routes, candidate)
	}
	return routes
}

func goPayAttemptEntryProxies(selected string, candidates []string) []string {
	selected = strings.TrimSpace(selected)
	routes := uniqueGoPayProxyRoutes(candidates)
	result := make([]string, 0, len(routes)+1)
	if selected != "" {
		result = append(result, selected)
	}
	for _, route := range routes {
		if sameRoute(route, selected) {
			continue
		}
		result = append(result, route)
	}
	return result
}
