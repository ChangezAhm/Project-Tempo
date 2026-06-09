// Lightweight client-side store for the MVP template library.
// Backed by localStorage so uploaded templates persist across reloads
// without a backend yet. This is where Supabase/Aspose parsing will plug
// in later (see docs/Project-Tempo.md).

export type Template = {
  id: string;
  name: string;
  fileName: string;
  sizeBytes: number;
  uploadedAt: string; // ISO string
  note?: string;
};

const STORAGE_KEY = "tempo.templates";

export function getTemplates(): Template[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as Template[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function saveTemplate(template: Template): void {
  if (typeof window === "undefined") return;
  const all = getTemplates();
  all.unshift(template);
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
}

export function deleteTemplate(id: string): void {
  if (typeof window === "undefined") return;
  const all = getTemplates().filter((t) => t.id !== id);
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
