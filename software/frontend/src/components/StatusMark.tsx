type StatusMarkProps = {
  tone: "normal" | "warning" | "danger" | "muted";
  label: string;
  shape?: "circle" | "diamond" | "square" | "clock";
};

export function StatusMark({ tone, label, shape = "circle" }: StatusMarkProps) {
  return (
    <span className="status-mark" data-tone={tone}>
      <i className={`status-shape ${shape}`} aria-hidden="true" />
      {label}
    </span>
  );
}
