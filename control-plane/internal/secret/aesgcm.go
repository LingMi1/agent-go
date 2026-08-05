// Package secret 提供列级密文封装：PAT 等敏感凭据用 AES-256-GCM 加密后入库，
// 明文绝不落列/日志（docs/16 §3.5）。主密钥来自 SECRET_MASTER_KEY（32 字节 base64）。
//
// 纯标准库（crypto/aes + cipher/gcm + crypto/rand）：无新重依赖。
// 封装格式 = nonce || ciphertext——nonce 每次 crypto/rand 新生成置于密文前，
// Open 时切出。GCM 自带认证：密文被篡改（含 nonce）时 Open 返回错误（不静默解出脏数据）。
package secret

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
)

// KeySize 是主密钥字节数（AES-256）。
const KeySize = 32

// ErrKeySize 表示主密钥长度不是 32 字节。
var ErrKeySize = errors.New("secret: master key must be 32 bytes")

// ErrCiphertextShort 表示密文短于一个 nonce（不可能是合法封装）。
var ErrCiphertextShort = errors.New("secret: ciphertext too short")

// DecodeMasterKey 解码并校验 base64 主密钥；空串返回 (nil,nil)（上层据此降级）。
func DecodeMasterKey(b64 string) ([]byte, error) {
	if b64 == "" {
		return nil, nil
	}
	key, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		return nil, fmt.Errorf("secret: decode master key: %w", err)
	}
	if len(key) != KeySize {
		return nil, ErrKeySize
	}
	return key, nil
}

// Seal 用 key 加密 plaintext，返回 nonce||ciphertext。每次调用生成新 nonce。
func Seal(key, plaintext []byte) ([]byte, error) {
	gcm, err := newGCM(key)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return nil, fmt.Errorf("secret: read nonce: %w", err)
	}
	// Seal 的 dst=nonce → 密文直接追加在 nonce 之后，返回 nonce||ct。
	return gcm.Seal(nonce, nonce, plaintext, nil), nil
}

// Open 反解 Seal 的输出。被篡改（GCM 认证失败）或截断时返回错误。
func Open(key, sealed []byte) ([]byte, error) {
	gcm, err := newGCM(key)
	if err != nil {
		return nil, err
	}
	ns := gcm.NonceSize()
	if len(sealed) < ns {
		return nil, ErrCiphertextShort
	}
	nonce, ct := sealed[:ns], sealed[ns:]
	pt, err := gcm.Open(nil, nonce, ct, nil)
	if err != nil {
		return nil, fmt.Errorf("secret: open (tampered or wrong key?): %w", err)
	}
	return pt, nil
}

func newGCM(key []byte) (cipher.AEAD, error) {
	if len(key) != KeySize {
		return nil, ErrKeySize
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("secret: new cipher: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("secret: new gcm: %w", err)
	}
	return gcm, nil
}
