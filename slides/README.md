# Slides — needquery (Slidev)

52 slides. Lime `#e4ff3e` on near-black `#0b0c10`, Inter + JetBrains Mono.

```bash
bun install
bun run dev        # present / edit at localhost:3030
bun run build      # static site -> dist/
```

Export to PDF (uses a system chromium, skips the browser download):

```bash
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
  bunx slidev export slides.md --output needquery.pdf \
  --executable-path "$(readlink -f "$(which chromium)")"
```
