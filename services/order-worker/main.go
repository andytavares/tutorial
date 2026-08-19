// Command order-worker consumes order events from Kafka and persists them to DynamoDB.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	ddbtypes "github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/twmb/franz-go/pkg/kgo"
)

var (
	processed = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "orders_processed_total",
		Help: "Order events consumed from Kafka and written to DynamoDB.",
	}, []string{"result"})

	processDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "order_process_duration_seconds",
		Help:    "Time to persist one order event.",
		Buckets: prometheus.DefBuckets,
	})

	lag = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "order_event_age_seconds",
		Help: "Age of the most recently processed event, from created_at to write time.",
	})
)

// putItemAPI is the part of *dynamodb.Client that this service uses. Depending
// on the interface rather than the concrete client is what lets the commit
// tests drive a failing write.
type putItemAPI interface {
	PutItem(ctx context.Context, params *dynamodb.PutItemInput, optFns ...func(*dynamodb.Options)) (*dynamodb.PutItemOutput, error)
}

// recordMarker is the part of *kgo.Client that this service uses to checkpoint
// progress. See AutoCommitMarks in main for why marking, not committing, is the
// per-record operation.
type recordMarker interface {
	MarkCommitRecords(rs ...*kgo.Record)
}

// partitionKey identifies one topic partition.
type partitionKey struct {
	topic     string
	partition int32
}

type orderEvent struct {
	OrderID     string `json:"order_id"`
	Customer    string `json:"customer"`
	SKU         string `json:"sku"`
	Quantity    int    `json:"quantity"`
	AmountCents int    `json:"amount_cents"`
	CreatedAt   string `json:"created_at"`
	S3Key       string `json:"s3_key"`
	Signature   string `json:"signature"`
}

type config struct {
	brokers []string
	topic   string
	group   string
	table   string
	region  string
	version string
	addr    string
}

func loadConfig() (config, error) {
	c := config{
		topic:   getenv("KAFKA_TOPIC", "orders"),
		group:   getenv("KAFKA_GROUP", "order-worker"),
		region:  getenv("AWS_DEFAULT_REGION", "us-east-1"),
		version: getenv("SERVICE_VERSION", "dev"),
		addr:    getenv("METRICS_ADDR", ":9090"),
	}
	brokers := os.Getenv("KAFKA_BROKERS")
	if brokers == "" {
		return c, errors.New("required environment variable KAFKA_BROKERS is not set")
	}
	c.brokers = strings.Split(brokers, ",")
	c.table = os.Getenv("DDB_TABLE")
	if c.table == "" {
		return c, errors.New("required environment variable DDB_TABLE is not set")
	}
	return c, nil
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	if err := run(); err != nil {
		slog.Error("order-worker exiting", "err", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := loadConfig()
	if err != nil {
		return fmt.Errorf("configuration error: %w", err)
	}

	// SIGTERM is what Kubernetes sends first on pod deletion. Handling it is the
	// difference between a graceful rolling update and dropped in-flight work.
	sigCtx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// A fatal error in a background goroutine cancels the same context the
	// consume loop runs on, and context.Cause carries the reason back out to
	// main so the process exits non-zero instead of consuming forever.
	ctx, cancel := context.WithCancelCause(sigCtx)
	defer cancel(nil)

	// AWS_ENDPOINT_URL is honoured natively by aws-sdk-go-v2, so pointing at Floci
	// needs no code change at all — the same binary runs against real AWS.
	awsCfg, err := awsconfig.LoadDefaultConfig(ctx, awsconfig.WithRegion(cfg.region))
	if err != nil {
		return fmt.Errorf("load aws config: %w", err)
	}
	ddb := dynamodb.NewFromConfig(awsCfg)

	// blocked holds the partitions whose commit is pinned behind a record that
	// failed to write. It is read and written only between PollFetches and
	// AllowRebalance, and in the revoke callback, which BlockRebalanceOnPoll
	// documents as mutually exclusive — so it needs no lock.
	blocked := make(map[partitionKey]bool)

	// Commit strategy, per franz-go's group_committing example ("marks" style):
	//
	//   - AutoCommitMarks: autocommitting commits only records handed to
	//     MarkCommitRecords, so an offset is committed only after its DynamoDB
	//     write returned success.
	//   - BlockRebalanceOnPoll: a non-empty poll blocks rebalances until
	//     AllowRebalance, so a commit can never land on a partition this member
	//     has already lost.
	//   - OnPartitionsRevoked: flush marked offsets before losing partitions.
	//     franz-go calls this on group leave too, which is what makes
	//     client.Close() commit on SIGTERM.
	//
	// Autocommitting stays on, so a marked offset is checkpointed even if the
	// synchronous commit below fails. Delivery is at-least-once: a crash between
	// the DynamoDB write and the commit replays the record, and PutItem on the
	// same order_id is idempotent, so replay is harmless.
	client, err := kgo.NewClient(
		kgo.SeedBrokers(cfg.brokers...),
		kgo.ConsumerGroup(cfg.group),
		kgo.ConsumeTopics(cfg.topic),
		kgo.ConsumeResetOffset(kgo.NewOffset().AtStart()),
		kgo.AutoCommitMarks(),
		kgo.BlockRebalanceOnPoll(),
		kgo.OnPartitionsRevoked(func(revokeCtx context.Context, cl *kgo.Client, revoked map[string][]int32) {
			if err := cl.CommitMarkedOffsets(revokeCtx); err != nil {
				slog.Error("revoke commit failed", "err", err)
			}
			// A revoked partition starts clean if this member is assigned it
			// again: the next owner resumes from the offset just committed.
			for topic, partitions := range revoked {
				for _, p := range partitions {
					delete(blocked, partitionKey{topic: topic, partition: p})
				}
			}
		}),
		kgo.SessionTimeout(30*time.Second),
	)
	if err != nil {
		return fmt.Errorf("create kafka client: %w", err)
	}
	defer client.Close()

	var ready atomic.Bool
	go func() {
		if err := serveHTTP(ctx, cfg, &ready); err != nil {
			cancel(fmt.Errorf("metrics server: %w", err))
		}
	}()

	if err := client.Ping(ctx); err != nil {
		return fmt.Errorf("kafka not reachable: %w", err)
	}
	ready.Store(true)
	slog.Info("order-worker started", "version", cfg.version, "topic", cfg.topic, "table", cfg.table)

	for {
		fetches := client.PollFetches(ctx)
		if ctx.Err() != nil {
			if cause := context.Cause(ctx); !errors.Is(cause, context.Canceled) {
				return cause
			}
			slog.Info("shutting down")
			return nil
		}
		if errs := fetches.Errors(); len(errs) > 0 {
			for _, e := range errs {
				slog.Error("fetch error", "topic", e.Topic, "partition", e.Partition, "err", e.Err)
			}
			client.AllowRebalance()
			continue
		}

		fetches.EachPartition(func(p kgo.FetchTopicPartition) {
			processPartition(ctx, ddb, cfg.table, client, blocked, p)
		})

		// CommitMarkedOffsets commits the marks made above and nothing else.
		// It runs before AllowRebalance so the commit cannot cross a rebalance.
		if err := client.CommitMarkedOffsets(ctx); err != nil {
			slog.Error("commit failed", "err", err)
		}
		client.AllowRebalance()
	}
}

// processPartition writes one partition's records to DynamoDB in offset order
// and marks a record for commit only once its write has succeeded.
//
// A Kafka commit is a single per-partition offset, so marking any later offset
// would commit past every earlier one. The first failed write therefore blocks
// the partition: nothing at or after that offset is ever marked, the committed
// offset stays behind the failed record, and a restart replays from there.
// Blocking persists across polls because marks cannot rewind — a mark made on a
// later batch would commit the record that was never written.
func processPartition(ctx context.Context, ddb putItemAPI, table string, m recordMarker, blocked map[partitionKey]bool, p kgo.FetchTopicPartition) {
	key := partitionKey{topic: p.Topic, partition: p.Partition}
	if blocked[key] {
		return
	}
	for _, r := range p.Records {
		if err := handle(ctx, ddb, table, r); err != nil {
			processed.WithLabelValues("error").Inc()
			slog.Error("failed to process record", "topic", r.Topic, "partition", r.Partition, "offset", r.Offset, "err", err)
			blocked[key] = true
			return
		}
		processed.WithLabelValues("ok").Inc()
		m.MarkCommitRecords(r)
	}
}

func handle(ctx context.Context, ddb putItemAPI, table string, r *kgo.Record) error {
	start := time.Now()
	defer func() { processDuration.Observe(time.Since(start).Seconds()) }()

	var ev orderEvent
	if err := json.Unmarshal(r.Value, &ev); err != nil {
		// A malformed message will never become valid. Skipping it (rather than
		// retrying forever) keeps the partition moving. In production this record
		// goes to a dead-letter topic instead of the floor.
		slog.Warn("skipping malformed record", "offset", r.Offset, "err", err)
		return nil
	}

	if ts, err := time.Parse(time.RFC3339, ev.CreatedAt); err == nil {
		lag.Set(time.Since(ts).Seconds())
	}

	writeCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	_, err := ddb.PutItem(writeCtx, &dynamodb.PutItemInput{
		TableName: aws.String(table),
		Item: map[string]ddbtypes.AttributeValue{
			"order_id":     &ddbtypes.AttributeValueMemberS{Value: ev.OrderID},
			"customer":     &ddbtypes.AttributeValueMemberS{Value: ev.Customer},
			"sku":          &ddbtypes.AttributeValueMemberS{Value: ev.SKU},
			"quantity":     &ddbtypes.AttributeValueMemberN{Value: strconv.Itoa(ev.Quantity)},
			"amount_cents": &ddbtypes.AttributeValueMemberN{Value: strconv.Itoa(ev.AmountCents)},
			"created_at":   &ddbtypes.AttributeValueMemberS{Value: ev.CreatedAt},
			"s3_key":       &ddbtypes.AttributeValueMemberS{Value: ev.S3Key},
			"signature":    &ddbtypes.AttributeValueMemberS{Value: ev.Signature},
		},
	})
	if err != nil {
		return fmt.Errorf("put item order_id=%s: %w", ev.OrderID, err)
	}
	slog.Info("persisted order", "order_id", ev.OrderID, "offset", r.Offset)
	return nil
}

// serveHTTP runs the metrics and probe endpoints until ctx is cancelled. It
// returns nil on a clean shutdown and an error otherwise; failing to bind the
// metrics port is fatal to the process, not something to log and ignore.
func serveHTTP(ctx context.Context, cfg config, ready *atomic.Bool) error {
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		if !ready.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"status":"not-ready"}`))
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"ready"}`))
	})

	srv := &http.Server{Addr: cfg.addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		<-ctx.Done()
		shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutCtx)
	}()
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return fmt.Errorf("listen on %s: %w", cfg.addr, err)
	}
	return nil
}
