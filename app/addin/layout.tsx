// Task-pane chrome: the add-in lives in a ~320px Excel side pane, so the
// site's desktop header/nav must not render here. The root layout puts the
// header as a direct child of <body>; this scoped style removes it for /addin
// only and gives the pane its own compact identity.
export default function AddinLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <>
      <style>{`body > header { display: none !important; }`}</style>
      <div className="flex min-h-screen flex-col bg-paper">
        <div className="flex items-center gap-2 border-b border-neutral-200/80 bg-white/70 px-4 py-2.5">
          <span className="h-2 w-2 rounded-sm bg-ink" />
          <span
            className="text-[15px] font-semibold tracking-tight text-ink"
            style={{ fontFamily: "var(--font-display)" }}
          >
            Tempo
          </span>
          <span className="ml-auto text-[10px] font-medium uppercase tracking-[0.16em] text-neutral-400">
            Excel
          </span>
        </div>
        <div className="min-w-0 flex-1">{children}</div>
      </div>
    </>
  );
}
