// Package gopayaddress supplies synthetic Indonesia billing addresses for the
// extracted GoPay CS flow. The catalog is embedded in source and has no
// external address service, database, network, or production-data dependency.
package gopayaddress

import (
	cryptorand "crypto/rand"
	"errors"
	"io"
	"math/big"
)

// Address is one synthetic Indonesia billing-address record.
type Address struct {
	ID         string
	Country    string
	Line1      string
	Line2      string
	City       string
	State      string
	PostalCode string
}

// Every street line is deliberately fictional. City, province, and postal
// fields are retained only to provide structurally plausible test fixtures.
var indonesiaCatalog = []Address{
	{ID: "synthetic-id-001", Country: "ID", Line1: "Jalan Contoh A No. 10", Line2: "Unit Uji 01", City: "Jakarta", State: "DKI Jakarta", PostalCode: "10310"},
	{ID: "synthetic-id-002", Country: "ID", Line1: "Jalan Contoh B No. 21", Line2: "Unit Uji 02", City: "Bandung", State: "Jawa Barat", PostalCode: "40115"},
	{ID: "synthetic-id-003", Country: "ID", Line1: "Jalan Contoh C No. 32", Line2: "Unit Uji 03", City: "Surabaya", State: "Jawa Timur", PostalCode: "60271"},
	{ID: "synthetic-id-004", Country: "ID", Line1: "Jalan Contoh D No. 43", Line2: "Unit Uji 04", City: "Medan", State: "Sumatera Utara", PostalCode: "20112"},
	{ID: "synthetic-id-005", Country: "ID", Line1: "Jalan Contoh E No. 54", Line2: "Unit Uji 05", City: "Denpasar", State: "Bali", PostalCode: "80232"},
	{ID: "synthetic-id-006", Country: "ID", Line1: "Jalan Contoh F No. 65", Line2: "Unit Uji 06", City: "Makassar", State: "Sulawesi Selatan", PostalCode: "90115"},
	{ID: "synthetic-id-007", Country: "ID", Line1: "Jalan Contoh G No. 76", Line2: "Unit Uji 07", City: "Semarang", State: "Jawa Tengah", PostalCode: "50241"},
	{ID: "synthetic-id-008", Country: "ID", Line1: "Jalan Contoh H No. 87", Line2: "Unit Uji 08", City: "Yogyakarta", State: "DI Yogyakarta", PostalCode: "55224"},
	{ID: "synthetic-id-009", Country: "ID", Line1: "Jalan Contoh I No. 98", Line2: "Unit Uji 09", City: "Balikpapan", State: "Kalimantan Timur", PostalCode: "76114"},
	{ID: "synthetic-id-010", Country: "ID", Line1: "Jalan Contoh J No. 19", Line2: "Unit Uji 10", City: "Palembang", State: "Sumatera Selatan", PostalCode: "30126"},
	{ID: "synthetic-id-011", Country: "ID", Line1: "Jalan Contoh K No. 28", Line2: "Unit Uji 11", City: "Manado", State: "Sulawesi Utara", PostalCode: "95111"},
	{ID: "synthetic-id-012", Country: "ID", Line1: "Jalan Contoh L No. 37", Line2: "Unit Uji 12", City: "Batam", State: "Kepulauan Riau", PostalCode: "29444"},
}

// Count returns the number of embedded records.
func Count() int {
	return len(indonesiaCatalog)
}

// At returns a copy of the record at index.
func At(index int) (Address, bool) {
	if index < 0 || index >= len(indonesiaCatalog) {
		return Address{}, false
	}
	return indonesiaCatalog[index], true
}

// Catalog returns a defensive copy of every embedded record.
func Catalog() []Address {
	return append([]Address(nil), indonesiaCatalog...)
}

// Random selects one record uniformly. A nil reader uses crypto/rand.Reader;
// callers may inject a deterministic reader in tests.
func Random(random io.Reader) (Address, error) {
	if len(indonesiaCatalog) == 0 {
		return Address{}, errors.New("GoPay synthetic address catalog is empty")
	}
	if random == nil {
		random = cryptorand.Reader
	}
	index, err := cryptorand.Int(random, big.NewInt(int64(len(indonesiaCatalog))))
	if err != nil {
		return Address{}, err
	}
	return indonesiaCatalog[index.Int64()], nil
}
