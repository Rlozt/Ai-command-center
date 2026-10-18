export function DiffView({ diff }: { diff: string }) {
  if (!diff.trim()) return <p className="muted">No textual changes.</p>;
  return (
    <pre className="diff" aria-label="diff">
      {diff.split("\n").map((line, i) => {
        const cls = line.startsWith("+++") || line.startsWith("---") ? "d-meta" : line.startsWith("+") ? "d-add" : line.startsWith("-") ? "d-del" : line.startsWith("@@") ? "d-hunk" : "";
        return (
          <span key={i} className={cls}>
            {line + "\n"}
          </span>
        );
      })}
    </pre>
  );
}
