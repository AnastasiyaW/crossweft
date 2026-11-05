// Command server is the shop's HTTP API.
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"

	"github.com/go-chi/chi/v5"
)

// APIVersion is the contract version the web client must send.
const APIVersion = "2026-09-01"

// APIVersionHeader carries APIVersion on every request.
const APIVersionHeader = "X-Shop-Api-Version"

// Order statuses. The worker writes the same values into the orders table.
const (
	StatusPending = "pending"
	StatusPaid    = "paid"
	StatusShipped = "shipped"
)

// Order is the JSON body of GET /v1/orders/{id}.
type Order struct {
	ID         string   `json:"id"`
	Items      []string `json:"items"`
	TotalCents int64    `json:"total_cents"`
	Status     string   `json:"status"`
}

func requireVersion(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.Printf("%s %s traceparent=%s", r.Method, r.URL.Path, r.Header.Get("traceparent"))
		if r.Header.Get(APIVersionHeader) != APIVersion {
			http.Error(w, "unsupported API version", http.StatusPreconditionFailed)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func getOrder(w http.ResponseWriter, r *http.Request) {
	order := Order{ID: chi.URLParam(r, "id"), Status: StatusPending}
	_ = json.NewEncoder(w).Encode(order)
}

func createOrder(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusCreated)
}

func main() {
	dsn := os.Getenv("SHOP_DB_URL")
	if dsn == "" {
		log.Fatal("SHOP_DB_URL is required")
	}
	r := chi.NewRouter()
	r.Get("/healthz", func(w http.ResponseWriter, r *http.Request) {})
	r.Route("/v1", func(v1 chi.Router) {
		v1.Use(requireVersion)
		v1.Get("/orders/{id}", getOrder)
		v1.Post("/orders", createOrder)
	})
	log.Fatal(http.ListenAndServe(os.Getenv("SHOP_LISTEN_ADDR"), r))
}
