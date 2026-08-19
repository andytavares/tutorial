package main

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	ddbtypes "github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
	"github.com/twmb/franz-go/pkg/kgo"
)

func TestLoadConfigRequiresBrokers(t *testing.T) {
	os.Clearenv()
	if _, err := loadConfig(); err == nil {
		t.Fatal("expected an error when KAFKA_BROKERS is unset")
	}
}

func TestLoadConfigDefaults(t *testing.T) {
	os.Clearenv()
	t.Setenv("KAFKA_BROKERS", "a:9092,b:9092")
	t.Setenv("DDB_TABLE", "orders")

	cfg, err := loadConfig()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(cfg.brokers) != 2 {
		t.Fatalf("expected 2 brokers, got %d", len(cfg.brokers))
	}
	if cfg.topic != "orders" {
		t.Fatalf("expected default topic 'orders', got %q", cfg.topic)
	}
	if cfg.group != "order-worker" {
		t.Fatalf("expected default group 'order-worker', got %q", cfg.group)
	}
}

var errWriteFailed = errors.New("dynamodb is having a day")

// fakeDDB records every PutItem it is asked to make and fails the ones whose
// order_id is listed in failOn.
type fakeDDB struct {
	failOn map[string]bool
	puts   []string
}

func (f *fakeDDB) PutItem(_ context.Context, params *dynamodb.PutItemInput, _ ...func(*dynamodb.Options)) (*dynamodb.PutItemOutput, error) {
	id := params.Item["order_id"].(*ddbtypes.AttributeValueMemberS).Value
	f.puts = append(f.puts, id)
	if f.failOn[id] {
		return nil, errWriteFailed
	}
	return &dynamodb.PutItemOutput{}, nil
}

// fakeMarker records the offsets handed to MarkCommitRecords. Those offsets are
// exactly what franz-go would commit, so asserting on them asserts on the
// commit boundary.
type fakeMarker struct{ marked []int64 }

func (m *fakeMarker) MarkCommitRecords(rs ...*kgo.Record) {
	for _, r := range rs {
		m.marked = append(m.marked, r.Offset)
	}
}

// partitionOf builds a one-partition fetch result holding one record per
// order_id, at consecutive offsets starting from 0.
func partitionOf(orderIDs ...string) kgo.FetchTopicPartition {
	p := kgo.FetchTopicPartition{Topic: "orders"}
	p.Partition = 3
	for i, id := range orderIDs {
		p.Records = append(p.Records, &kgo.Record{
			Topic:     "orders",
			Partition: 3,
			Offset:    int64(i),
			Value:     []byte(`{"order_id":"` + id + `","quantity":1,"amount_cents":100}`),
		})
	}
	return p
}

func equalOffsets(got, want []int64) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}

func TestProcessPartitionMarksEveryRecordThatWasWritten(t *testing.T) {
	ddb := &fakeDDB{}
	m := &fakeMarker{}
	blocked := map[partitionKey]bool{}

	processPartition(context.Background(), ddb, "orders", m, blocked, partitionOf("a", "b", "c"))

	if !equalOffsets(m.marked, []int64{0, 1, 2}) {
		t.Fatalf("expected offsets 0,1,2 to be marked, got %v", m.marked)
	}
	if len(blocked) != 0 {
		t.Fatalf("expected no blocked partitions, got %v", blocked)
	}
}

// This is the regression test for the commit semantics: a failed DynamoDB write
// must stop the commit boundary dead. If someone reintroduces marking (and so
// committing) records at or past a failed offset, this fails.
func TestProcessPartitionNeverMarksAtOrPastAFailedWrite(t *testing.T) {
	ddb := &fakeDDB{failOn: map[string]bool{"b": true}}
	m := &fakeMarker{}
	blocked := map[partitionKey]bool{}
	key := partitionKey{topic: "orders", partition: 3}

	processPartition(context.Background(), ddb, "orders", m, blocked, partitionOf("a", "b", "c"))

	if !equalOffsets(m.marked, []int64{0}) {
		t.Fatalf("only offset 0 was written successfully, but offsets %v were marked for commit", m.marked)
	}
	if !blocked[key] {
		t.Fatal("expected the partition to be blocked after a failed write")
	}
}

// Marks cannot rewind, so once a partition is blocked a later batch must not
// mark it either — that would commit past the record that was never written.
func TestProcessPartitionStaysBlockedOnLaterBatches(t *testing.T) {
	ddb := &fakeDDB{failOn: map[string]bool{"a": true}}
	m := &fakeMarker{}
	blocked := map[partitionKey]bool{}

	processPartition(context.Background(), ddb, "orders", m, blocked, partitionOf("a"))
	if len(m.marked) != 0 {
		t.Fatalf("expected nothing marked, got %v", m.marked)
	}

	ddb.failOn = nil
	processPartition(context.Background(), ddb, "orders", m, blocked, partitionOf("d", "e"))

	if len(m.marked) != 0 {
		t.Fatalf("a blocked partition must not be marked on a later batch, got %v", m.marked)
	}
	if len(ddb.puts) != 1 {
		t.Fatalf("a blocked partition must not be written again, got puts %v", ddb.puts)
	}
}

// A blocked partition is only cleared by a revoke, which happens outside this
// function; clearing the entry lets the partition make progress again.
func TestProcessPartitionResumesOnceUnblocked(t *testing.T) {
	ddb := &fakeDDB{failOn: map[string]bool{"a": true}}
	m := &fakeMarker{}
	blocked := map[partitionKey]bool{}
	key := partitionKey{topic: "orders", partition: 3}

	processPartition(context.Background(), ddb, "orders", m, blocked, partitionOf("a"))
	delete(blocked, key)

	ddb.failOn = nil
	processPartition(context.Background(), ddb, "orders", m, blocked, partitionOf("f"))

	if !equalOffsets(m.marked, []int64{0}) {
		t.Fatalf("expected offset 0 marked after unblocking, got %v", m.marked)
	}
}

// A malformed record can never become valid, so handle skips it. It must still
// be marked, or one bad message would pin the partition's commit forever.
func TestProcessPartitionMarksMalformedRecords(t *testing.T) {
	ddb := &fakeDDB{}
	m := &fakeMarker{}
	blocked := map[partitionKey]bool{}

	p := partitionOf("a")
	p.Records[0].Value = []byte("{not json")

	processPartition(context.Background(), ddb, "orders", m, blocked, p)

	if !equalOffsets(m.marked, []int64{0}) {
		t.Fatalf("expected the malformed record to be marked, got %v", m.marked)
	}
	if len(ddb.puts) != 0 {
		t.Fatalf("expected no write for a malformed record, got puts %v", ddb.puts)
	}
}

func TestHandleWrapsPutItemError(t *testing.T) {
	ddb := &fakeDDB{failOn: map[string]bool{"a": true}}
	r := partitionOf("a").Records[0]

	err := handle(context.Background(), ddb, "orders", r)
	if err == nil {
		t.Fatal("expected an error")
	}
	if !errors.Is(err, errWriteFailed) {
		t.Fatalf("expected the underlying error to be wrapped, got %v", err)
	}
	if !strings.Contains(err.Error(), "order_id=a") {
		t.Fatalf("expected the error to name the order, got %q", err.Error())
	}
}
