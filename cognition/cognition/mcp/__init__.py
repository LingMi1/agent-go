"""MCP 运行时：三传输连接、装配期预热、工具发现缓存、per-server 串行。

纯逻辑（config / naming / serial）与 I/O（connection / adapter / registry）物理分离：
前者零外部依赖、可 TDD；后者用 in-memory fake server 做契约测试。
"""
