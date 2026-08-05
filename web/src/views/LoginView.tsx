import { useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

// D3（docs/17）：密码登录 + 自助注册切换。前端 gate 非安全边界——真校验在后端（bcrypt +
// requireAdmin）。提交 async：成功由上层跳转，失败展示后端 message（用户名或密码错误 / 已被占用）。
export function LoginView({
  onLogin,
  onRegister,
}: {
  onLogin: (username: string, password: string) => Promise<void>;
  onRegister: (username: string, password: string) => Promise<void>;
}) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const canSubmit = username.trim().length > 0 && password.length >= 6 && !busy;

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError("");
    try {
      if (mode === "login") await onLogin(username.trim(), password);
      else await onRegister(username.trim(), password);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Operation failed. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full items-center justify-center p-6">
      <Card className="w-full max-w-sm rounded-xl border bg-card shadow-sm" data-testid="login-card">
        <CardHeader>
          <CardTitle className="font-display text-2xl font-semibold">
            Agent-go
          </CardTitle>
          <CardDescription>
            {mode === "login" ? "Sign in to your multi-agent workspace" : "Create a new account"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="Username"
            autoComplete="username"
            className="mb-3"
            data-testid="login-username"
          />
          <Input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void submit()}
            placeholder="Password (min 6 characters)"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            className="mb-3"
            data-testid="login-password"
          />
          {error && (
            <div className="mb-3 text-xs text-destructive" data-testid="login-error">
              {error}
            </div>
          )}
          <Button
            onClick={() => void submit()}
            disabled={!canSubmit}
            className="w-full"
            data-testid="login-submit"
          >
            {busy && <Loader2 className="mr-1 size-4 animate-spin" />}
            {mode === "login" ? "Sign In" : "Sign Up"}
          </Button>
          <button
            type="button"
            onClick={() => {
              setMode((m) => (m === "login" ? "register" : "login"));
              setError("");
            }}
            className="mt-4 w-full text-center text-xs text-muted-foreground/70 hover:text-foreground"
            data-testid="login-toggle"
          >
            {mode === "login" ? "Don't have an account? Sign up" : "Already have an account? Sign in"}
          </button>
          <div className="mt-4 text-[11px] leading-relaxed text-muted-foreground/60">
            Your account (owner role) manages runs, artifacts, and knowledge base access. Admins can manage users and monitor system usage.
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
