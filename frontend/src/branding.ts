// ---------------------------------------------------------------------------
// Organization branding helpers (logo upload + display fallback).
//
// The tenant logo is stored on the tenant as a small self-contained data URL
// (so it needs no file host) and rendered in the sidebar next to the Kavachio
// brand. Raster uploads are downscaled client-side to keep the payload small;
// SVGs are kept as-is to stay crisp. The localStorage cache for the sidebar
// lives in auth.ts alongside the other identity keys.
// ---------------------------------------------------------------------------

/** Two-letter fallback used when an organization has no logo yet. */
export function initials(name?: string | null): string {
  if (!name) return "··";
  const parts = name.trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "··";
}

/**
 * Read an image File and resolve to a data URL suitable for storing on the
 * tenant and rendering in the sidebar. Raster images are downscaled so the
 * longest edge is at most `maxPx`; SVGs are returned unchanged.
 */
export function fileToLogoDataUrl(file: File, maxPx = 256): Promise<string> {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onerror = () => reject(new Error("Could not read that file."));
    fr.onload = () => {
      const raw = fr.result as string;
      // Keep vector logos as-is — rasterizing them would lose their sharpness.
      if (file.type === "image/svg+xml") return resolve(raw);
      const img = new Image();
      img.onload = () => {
        const scale = Math.min(1, maxPx / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const cv = document.createElement("canvas");
        cv.width = w; cv.height = h;
        const ctx = cv.getContext("2d");
        if (!ctx) return resolve(raw);
        ctx.drawImage(img, 0, 0, w, h);
        try { resolve(cv.toDataURL("image/png")); }
        catch { resolve(raw); } // tainted canvas etc. — fall back to the raw URL
      };
      img.onerror = () => resolve(raw); // unknown format — store what we read
      img.src = raw;
    };
    fr.readAsDataURL(file);
  });
}
