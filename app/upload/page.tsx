"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { formatSize, uploadTemplate } from "@/lib/templates";

const ACCEPT = ".xlsx,.xls,.xlsm";

export default function UploadPage() {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function pickFile(f: File | null) {
    if (!f) return;
    setFile(f);
    if (!name) setName(f.name.replace(/\.(xlsx|xls|xlsm)$/i, ""));
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    pickFile(e.dataTransfer.files?.[0] ?? null);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!file || uploading) return;
    setUploading(true);
    setError(null);
    try {
      await uploadTemplate({
        file,
        name: name.trim() || file.name,
        note: note.trim() || undefined,
      });
      router.push("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
      setUploading(false);
    }
  }

  return (
    <div className="mx-auto max-w-xl">
      <Link href="/" className="text-sm text-neutral-400 transition hover:text-neutral-700">
        ← Back to library
      </Link>

      <h1 className="mt-4 text-2xl font-semibold tracking-tight">Upload template</h1>
      <p className="mt-1 text-sm text-neutral-500">
        Add a sponsor Excel template to the library.
      </p>

      <form onSubmit={handleSubmit} className="mt-8 space-y-6">
        <div
          onClick={() => inputRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          className={`flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-12 text-center transition ${
            dragging
              ? "border-neutral-900 bg-neutral-100"
              : "border-neutral-300 bg-white hover:border-neutral-400"
          }`}
        >
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPT}
            className="hidden"
            onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
          />
          {file ? (
            <>
              <div className="inline-flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-50 text-sm font-semibold text-emerald-600">
                XLS
              </div>
              <p className="mt-3 font-medium">{file.name}</p>
              <p className="text-xs text-neutral-400">{formatSize(file.size)}</p>
              <p className="mt-2 text-xs text-neutral-400">Click to choose a different file</p>
            </>
          ) : (
            <>
              <div className="inline-flex h-10 w-10 items-center justify-center rounded-lg bg-neutral-100 text-neutral-400">
                ↑
              </div>
              <p className="mt-3 text-sm font-medium">Drop an Excel file here, or click to browse</p>
              <p className="text-xs text-neutral-400">.xlsx, .xls, .xlsm</p>
            </>
          )}
        </div>

        <div>
          <label htmlFor="name" className="mb-1 block text-sm font-medium">
            Template name
          </label>
          <input
            id="name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Monthly Flash Report"
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm outline-none transition focus:border-neutral-900"
          />
        </div>

        <div>
          <label htmlFor="note" className="mb-1 block text-sm font-medium">
            Context <span className="font-normal text-neutral-400">(optional)</span>
          </label>
          <textarea
            id="note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={3}
            placeholder="Notes the system should remember about this template…"
            className="w-full resize-none rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm outline-none transition focus:border-neutral-900"
          />
        </div>

        {error ? (
          <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-600">{error}</p>
        ) : null}

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={!file || uploading}
            className="rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {uploading ? "Uploading…" : "Add to library"}
          </button>
          <Link
            href="/"
            className="rounded-md px-4 py-2 text-sm font-medium text-neutral-500 transition hover:text-neutral-900"
          >
            Cancel
          </Link>
        </div>
      </form>
    </div>
  );
}
