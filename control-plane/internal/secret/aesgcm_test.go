package secret_test

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"testing"

	"my-agent/control-plane/internal/secret"
)

func testKey(t *testing.T) []byte {
	t.Helper()
	k := make([]byte, secret.KeySize)
	if _, err := rand.Read(k); err != nil {
		t.Fatalf("rand key: %v", err)
	}
	return k
}

// 往返：Seal 后 Open 得回明文；两次 Seal 同明文密文不同（nonce 每次新生成）。
func TestSealOpenRoundtrip(t *testing.T) {
	key := testKey(t)
	pt := []byte("ghp_supersecrettoken_1234567890")

	ct1, err := secret.Seal(key, pt)
	if err != nil {
		t.Fatalf("seal: %v", err)
	}
	got, err := secret.Open(key, ct1)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	if !bytes.Equal(got, pt) {
		t.Fatalf("roundtrip mismatch: %q != %q", got, pt)
	}
	// 密文里不得含明文（起码不是首段拼接）。
	if bytes.Contains(ct1, pt) {
		t.Fatal("密文包含明文子串")
	}
	ct2, _ := secret.Seal(key, pt)
	if bytes.Equal(ct1, ct2) {
		t.Fatal("两次 Seal 密文相同（nonce 未随机）")
	}
}

// 篡改检测：改一个字节 → GCM 认证失败，Open 报错（不静默解出脏数据）。
func TestOpenTamperDetected(t *testing.T) {
	key := testKey(t)
	ct, _ := secret.Seal(key, []byte("token"))
	ct[len(ct)-1] ^= 0xFF // 翻转末字节（密文体）
	if _, err := secret.Open(key, ct); err == nil {
		t.Fatal("篡改密文应 Open 失败")
	}
	// 翻转 nonce 区（前段）同样应失败。
	ct2, _ := secret.Seal(key, []byte("token"))
	ct2[0] ^= 0xFF
	if _, err := secret.Open(key, ct2); err == nil {
		t.Fatal("篡改 nonce 应 Open 失败")
	}
}

// 错误密钥无法解出。
func TestOpenWrongKey(t *testing.T) {
	ct, _ := secret.Seal(testKey(t), []byte("token"))
	if _, err := secret.Open(testKey(t), ct); err == nil {
		t.Fatal("错误密钥应 Open 失败")
	}
}

// 边界：非法密钥长度、过短密文。
func TestErrors(t *testing.T) {
	if _, err := secret.Seal(make([]byte, 16), []byte("x")); err == nil {
		t.Fatal("16 字节密钥应报错")
	}
	if _, err := secret.Open(testKey(t), []byte("short")); err == nil {
		t.Fatal("过短密文应报错")
	}
}

// DecodeMasterKey：空串 → (nil,nil)（降级信号）；合法 32 字节 base64 → key；长度错 → 错误。
func TestDecodeMasterKey(t *testing.T) {
	if k, err := secret.DecodeMasterKey(""); err != nil || k != nil {
		t.Fatalf("空串应 (nil,nil)，得 %v %v", k, err)
	}
	good := base64.StdEncoding.EncodeToString(testKey(t))
	k, err := secret.DecodeMasterKey(good)
	if err != nil || len(k) != secret.KeySize {
		t.Fatalf("合法 key 解码失败: %v %d", err, len(k))
	}
	short := base64.StdEncoding.EncodeToString(make([]byte, 16))
	if _, err := secret.DecodeMasterKey(short); err == nil {
		t.Fatal("16 字节应报长度错")
	}
	if _, err := secret.DecodeMasterKey("!!!not base64!!!"); err == nil {
		t.Fatal("坏 base64 应报错")
	}
}
