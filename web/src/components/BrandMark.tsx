/** Agent-go brand mark: crescent moon.
 *
 *  Uses `currentColor` so the caller controls the colour via className/text colour.
 *  Default size is `size-8`.
 */
export function BrandMark({ className = "size-8" }: { className?: string }) {
  return (
    <svg viewBox="0 0 36 36" fill="none" className={className}>
      <circle cx="18" cy="18" r="14" stroke="currentColor" strokeWidth="2.2" />
      <circle cx="23" cy="13" r="10.5" fill="currentColor" stroke="none" opacity="0.9" />
    </svg>
  );
}
