/**
 * Work around a Chromium bug that makes a mouse drag-selection silently fail
 * when the pointer position has a fractional part (Retina / trackpad input).
 *
 * On mousedown Blink hit-tests at the *rounded* pointer position and places a
 * caret there (`event_handling_util::ContentPointFromRootFrame` →
 * `PhysicalOffset::FromPointFRound`). When the drag starts it re-hit-tests at
 * the stored `mouse_down_pos_`, which was *floored*
 * (`MouseEventManager::HandleMousePressEvent` → `gfx::ToFlooredPoint`). If a
 * character boundary falls between those two integer x's, the re-hit-test
 * yields a different caret, `FrameSelection::SetSelection` sees a change, and
 * `SelectionController::NotifySelectionChanged` drops the drag state back to
 * "placed caret" — so every subsequent mousemove just collapses the caret at
 * the pointer instead of extending from the anchor. Nothing ever highlights.
 * In 14px monospace prose that is roughly one drag in fifteen; the user reads
 * it as "I can't select this", retries within the double-click interval, and
 * the retry is then a word- or paragraph-granularity drag that grabs far more
 * than they wanted.
 *
 * The fix: right after Blink has placed its caret, move it to the caret the
 * drag will re-hit-test to (`caretRangeFromPoint` at the floored point). The
 * drag's re-hit-test then finds the selection already there, `SetSelection`
 * is a no-op, the drag state survives, and the selection extends normally.
 * When the two positions agree this is a no-op too. Only ever touches a
 * collapsed selection, so word/paragraph selection (double/triple click),
 * shift-click extension and drag-of-selected-text are untouched.
 */

const TEXT_CONTROL_SELECTOR = 'input, textarea, select, [contenteditable=""], [contenteditable="true"]';

/** Attach to a transcript container. Returns the detach function. */
export function attachSelectionDragFix(container: HTMLElement): () => void {
  const onMouseDown = (event: MouseEvent) => {
    // Plain left button only: modifiers and multi-clicks have their own
    // selection semantics that must not be disturbed.
    if (event.button !== 0 || event.detail > 1 || event.shiftKey || event.metaKey || event.ctrlKey) return;
    if (typeof document.caretRangeFromPoint !== 'function') return;
    const target = event.target instanceof Element ? event.target : null;
    // Never move the document selection out from under a focused text control.
    if (target?.closest(TEXT_CONTROL_SELECTOR)) return;
    const x = Math.floor(event.clientX);
    const y = Math.floor(event.clientY);
    // Blink places its caret as the default action, after listeners have run,
    // so defer to the next task. A drag's first mousemove is a later input
    // task, so this lands in between.
    setTimeout(() => {
      const selection = window.getSelection();
      if (!selection || selection.rangeCount === 0 || !selection.isCollapsed) return;
      if (document.activeElement?.closest(TEXT_CONTROL_SELECTOR)) return;
      const range = document.caretRangeFromPoint(x, y);
      if (!range || !container.contains(range.startContainer)) return;
      selection.collapse(range.startContainer, range.startOffset);
    }, 0);
  };
  container.addEventListener('mousedown', onMouseDown, { passive: true });
  return () => container.removeEventListener('mousedown', onMouseDown);
}
