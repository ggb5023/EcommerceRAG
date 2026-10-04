package main

import (
	"context"
	"testing"
)

func TestIngestWorkerTestHoldDisabledOutsideTest(t *testing.T) {
	t.Setenv("APP_ENV", "development")
	t.Setenv("INGEST_WORKER_TEST_HOLD_MS", "invalid")
	if err := ingestWorkerTestHold(context.Background()); err != nil {
		t.Fatalf("test hold must be ignored outside APP_ENV=test: %v", err)
	}
}

func TestIngestWorkerTestHoldRejectsInvalidValue(t *testing.T) {
	t.Setenv("APP_ENV", "test")
	t.Setenv("INGEST_WORKER_TEST_HOLD_MS", "0")
	if err := ingestWorkerTestHold(context.Background()); err == nil {
		t.Fatal("expected invalid test hold to be rejected")
	}
}

func TestIngestWorkerTestHoldCanBeCancelled(t *testing.T) {
	t.Setenv("APP_ENV", "test")
	t.Setenv("INGEST_WORKER_TEST_HOLD_MS", "1000")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := ingestWorkerTestHold(ctx); err != context.Canceled {
		t.Fatalf("expected context cancellation, got %v", err)
	}
}
