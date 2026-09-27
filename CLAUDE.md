# CLAUDE.md - Project Context & Guidelines

## 🛠 Build & Test Commands
- **Rust (Systems Backend)**: `cargo build`, `cargo test`, `cargo clippy`
- **Go (Services Backend)**: `go build ./...`, `go test ./...`, `golangci-lint run`
- **Python (AI/Data Layer)**: `uv run pytest`, `uv run ruff check` (or `poetry`/`pip`)
- **Next.js (Frontend)**: `npm run dev`, `npm run build`, `npm run lint`
- **Full Stack Pre-Commit**: `cargo check && go test ./... && npm run lint`

## 🧠 Code Style & Architecture

### 🐹 Go (Microservices / API Network Layer)
- **Style**: Idiomatic Go (follow `go fmt` and `uber-go/guide`). 
- **Error Handling**: Handle errors explicitly (`if err != nil { return fmt.Errorf("...", err) }`). Never ignore errors.
- **Concurrency**: Use channels and `sync.WaitGroup` cleanly. Avoid unbounded goroutines; always pass `context.Context` for cancellation/timeouts.
- **Dependencies**: Use standard library where possible. Use `chi` or `gin` for routing if specified in `go.mod`.

### 🦀 Rust (Performance Engine / Core Compute)
- **Style**: Strict adherence to idiomatic Rust. Prefer `Result<T, E>` over `unwrap()`.
- **Error Handling**: Use `thiserror` for library crates, `anyhow` for application binaries.
- **Crates**: Check `Cargo.toml` before suggesting new dependencies.

### 🐍 Python (AI / Data Scripts)
- **Type Safety**: Strict Typle Checking. Use `def func(x: int) -> str:` syntax.
- **Style**: Follow PEP 8 rules. Use `ruff` for linting.
- **Data Validation**: Use `pydantic` for data schemas and parsing.

### ⚛️ Next.js (Frontend UI)
- **Framework**: App Router (`app/` directory). Utilize React Server Components (RSC) by default.
- **State & Fetching**: Use Server Actions or `tanstack-query` for data. Avoid raw `useEffect` fetches.
- **Types**: Strict TypeScript only. Absolutely zero `any` usage. Validate APIs using `zod`.

## ⚡️ Workflow Rules
1. **One Step at a Time**: When modifying multi-layer features, finish the Backend changes (Go/Rust), verify them via tests, *then* update the Next.js Frontend.
2. **No Hallucinations**: If you need a structural type definition, run `grep`, `cat`, or use Go/Rust definition lookups. Do not invent schemas.
