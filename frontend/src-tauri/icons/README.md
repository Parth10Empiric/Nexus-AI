# App Icons

Tauri needs icon files referenced in `tauri.conf.json`. Generate them once
from any square PNG/SVG (≥ 512×512) with:

```bash
npm run tauri icon path/to/your-logo.png
```

This populates `32x32.png`, `128x128.png`, `icon.png`, `.ico`, and `.icns`
automatically. Until you run it, use the Tauri default icons or a placeholder.
