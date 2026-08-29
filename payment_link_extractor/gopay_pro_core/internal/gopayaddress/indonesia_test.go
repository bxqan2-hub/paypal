package gopayaddress

import (
	"bytes"
	"testing"
)

func TestCatalogContainsOnlyCompleteSyntheticIndonesiaAddresses(t *testing.T) {
	records := Catalog()
	if len(records) < 10 {
		t.Fatalf("catalog size = %d, want at least 10", len(records))
	}
	seen := make(map[string]struct{}, len(records))
	for _, record := range records {
		if record.ID == "" || record.Country != "ID" || record.Line1 == "" ||
			record.City == "" || record.State == "" || record.PostalCode == "" {
			t.Fatalf("incomplete address: %#v", record)
		}
		if _, exists := seen[record.ID]; exists {
			t.Fatalf("duplicate address ID: %s", record.ID)
		}
		seen[record.ID] = struct{}{}
	}

	records[0].Line1 = "mutated"
	first, ok := At(0)
	if !ok || first.Line1 == "mutated" {
		t.Fatal("Catalog did not return a defensive copy")
	}
}

func TestRandomSupportsInjectedReader(t *testing.T) {
	got, err := Random(bytes.NewReader(make([]byte, 32)))
	if err != nil {
		t.Fatalf("Random() error = %v", err)
	}
	want, ok := At(0)
	if !ok || got != want {
		t.Fatalf("Random() = %#v, want %#v", got, want)
	}
}

func TestAtRejectsOutOfRangeIndex(t *testing.T) {
	if _, ok := At(-1); ok {
		t.Fatal("At(-1) succeeded")
	}
	if _, ok := At(Count()); ok {
		t.Fatal("At(Count()) succeeded")
	}
}
