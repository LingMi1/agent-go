package artifact

import (
	"bytes"
	"context"
	"errors"
	"io"
	"strings"
	"testing"
)

// fakeStore is an in-memory Store for testing.
type fakeStore struct {
	objects map[string]*Object
	bucket  bool
}

func newFakeStore() *fakeStore {
	return &fakeStore{objects: make(map[string]*Object)}
}

func (f *fakeStore) EnsureBucket(ctx context.Context) error {
	f.bucket = true
	return nil
}

func (f *fakeStore) Put(ctx context.Context, key string, body io.Reader, size int64, contentType string) error {
	data, err := io.ReadAll(body)
	if err != nil {
		return err
	}
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	f.objects[key] = &Object{
		Body:        io.NopCloser(bytes.NewReader(data)),
		ContentType: contentType,
		Size:        int64(len(data)),
	}
	return nil
}

func (f *fakeStore) Open(ctx context.Context, key string) (*Object, error) {
	obj, ok := f.objects[key]
	if !ok {
		return nil, ErrNotFound
	}
	// Return a copy with a fresh reader (original was consumed).
	return &Object{
		Body:        io.NopCloser(bytes.NewReader(f.objectData(key))),
		ContentType: obj.ContentType,
		Size:        obj.Size,
	}, nil
}

func (f *fakeStore) objectData(key string) []byte {
	obj, ok := f.objects[key]
	if !ok {
		return nil
	}
	data, _ := io.ReadAll(obj.Body)
	obj.Body = io.NopCloser(bytes.NewReader(data))
	return data
}

func TestStoreOpen_NotFound(t *testing.T) {
	s := newFakeStore()
	_, err := s.Open(t.Context(), "nonexistent")
	if !errors.Is(err, ErrNotFound) {
		t.Errorf("expected ErrNotFound, got %v", err)
	}
}

func TestStoreOpen_NotFoundAfterPut(t *testing.T) {
	s := newFakeStore()
	err := s.Put(t.Context(), "existing", strings.NewReader("hello"), 5, "text/plain")
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	_, err = s.Open(t.Context(), "other")
	if !errors.Is(err, ErrNotFound) {
		t.Errorf("expected ErrNotFound for non-existent key, got %v", err)
	}
}

func TestStorePutAndOpen(t *testing.T) {
	s := newFakeStore()
	body := "hello world"
	err := s.Put(t.Context(), "doc.txt", strings.NewReader(body), int64(len(body)), "text/plain")
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	obj, err := s.Open(t.Context(), "doc.txt")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer obj.Body.Close()

	if obj.ContentType != "text/plain" {
		t.Errorf("ContentType = %q, want text/plain", obj.ContentType)
	}
	if obj.Size != int64(len(body)) {
		t.Errorf("Size = %d, want %d", obj.Size, len(body))
	}
	data, err := io.ReadAll(obj.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	if string(data) != body {
		t.Errorf("body = %q, want %q", string(data), body)
	}
}

func TestStorePut_EmptyContentType(t *testing.T) {
	s := newFakeStore()
	err := s.Put(t.Context(), "bin", strings.NewReader("data"), 4, "")
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	obj, err := s.Open(t.Context(), "bin")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer obj.Body.Close()
	if obj.ContentType != "application/octet-stream" {
		t.Errorf("ContentType = %q, want application/octet-stream", obj.ContentType)
	}
}

func TestStorePut_ZeroLength(t *testing.T) {
	s := newFakeStore()
	err := s.Put(t.Context(), "empty", strings.NewReader(""), 0, "text/plain")
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	obj, err := s.Open(t.Context(), "empty")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer obj.Body.Close()
	if obj.Size != 0 {
		t.Errorf("Size = %d, want 0", obj.Size)
	}
	data, err := io.ReadAll(obj.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	if len(data) != 0 {
		t.Errorf("body length = %d, want 0", len(data))
	}
}

func TestStorePut_Overwrite(t *testing.T) {
	s := newFakeStore()
	err := s.Put(t.Context(), "key", strings.NewReader("v1"), 2, "text/plain")
	if err != nil {
		t.Fatalf("first Put: %v", err)
	}
	err = s.Put(t.Context(), "key", strings.NewReader("v2-updated"), 10, "text/plain")
	if err != nil {
		t.Fatalf("second Put: %v", err)
	}
	obj, err := s.Open(t.Context(), "key")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer obj.Body.Close()
	data, _ := io.ReadAll(obj.Body)
	if string(data) != "v2-updated" {
		t.Errorf("body = %q, want v2-updated", string(data))
	}
}

func TestStoreEnsureBucket(t *testing.T) {
	s := newFakeStore()
	if err := s.EnsureBucket(t.Context()); err != nil {
		t.Fatalf("EnsureBucket: %v", err)
	}
	if !s.bucket {
		t.Error("bucket not marked as ensured")
	}
}

func TestStoreEnsureBucket_Idempotent(t *testing.T) {
	s := newFakeStore()
	if err := s.EnsureBucket(t.Context()); err != nil {
		t.Fatalf("first EnsureBucket: %v", err)
	}
	if err := s.EnsureBucket(t.Context()); err != nil {
		t.Fatalf("second EnsureBucket: %v", err)
	}
}

func TestObjectBodyClose(t *testing.T) {
	s := newFakeStore()
	err := s.Put(t.Context(), "key", strings.NewReader("data"), 4, "text/plain")
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	obj, err := s.Open(t.Context(), "key")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	// Body should be a valid io.ReadCloser and close without error.
	data, err := io.ReadAll(obj.Body)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(data) != "data" {
		t.Errorf("body = %q, want data", string(data))
	}
	if err := obj.Body.Close(); err != nil {
		t.Errorf("close: %v", err)
	}
}

func TestStorePut_LargeContentType(t *testing.T) {
	s := newFakeStore()
	ct := "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
	err := s.Put(t.Context(), "sheet.xlsx", strings.NewReader("binary"), 6, ct)
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	obj, err := s.Open(t.Context(), "sheet.xlsx")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer obj.Body.Close()
	if obj.ContentType != ct {
		t.Errorf("ContentType = %q, want %q", obj.ContentType, ct)
	}
}

func TestStoreOpen_KeyWithPath(t *testing.T) {
	s := newFakeStore()
	key := "runs/abc123/report.pdf"
	err := s.Put(t.Context(), key, strings.NewReader("pdf"), 3, "application/pdf")
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	obj, err := s.Open(t.Context(), key)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer obj.Body.Close()
	if obj.ContentType != "application/pdf" {
		t.Errorf("ContentType = %q, want application/pdf", obj.ContentType)
	}
}

// Compile-time check: fakeStore satisfies Store.
var _ Store = (*fakeStore)(nil)
