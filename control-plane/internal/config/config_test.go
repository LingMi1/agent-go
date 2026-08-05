package config

import "testing"

// EnvBool 必须认文档一律使用的启用值 X=1，同时兼容既有 =true/大小写变体，
// 并把空/未设置/0/false/no/off 判为假——否则「AUTH_REQUIRED=1 静默不生效」类部署陷阱复现。
func TestEnvBool(t *testing.T) {
	const key = "MY_AGENT_TEST_ENVBOOL"

	truthy := []string{"1", "true", "TRUE", "True", "yes", "YES", "on", "ON", " on ", "\ttrue\n"}
	for _, v := range truthy {
		t.Setenv(key, v)
		if !EnvBool(key) {
			t.Errorf("EnvBool(%q) = false, want true", v)
		}
	}

	falsy := []string{"", "false", "FALSE", "0", "no", "off", "banana", "  "}
	for _, v := range falsy {
		t.Setenv(key, v)
		if EnvBool(key) {
			t.Errorf("EnvBool(%q) = true, want false", v)
		}
	}
}
