export type CommandEvent = {
  id: string;
  task_id?: number | null;
  event_type: string;
  message: string;
  created_at: string;
};

export function applyEvents(current: CommandEvent[], incoming: CommandEvent[]): CommandEvent[] {
  const seen = new Set(current.map((item) => item.id));
  const next = current.slice();
  for (const event of incoming) {
    if (seen.has(event.id)) continue;
    seen.add(event.id);
    next.push(event);
  }
  return next;
}

export function shouldRefetch(previousCursor: string, nextCursor: string): boolean {
  return Boolean(previousCursor) && nextCursor.length > 0 && nextCursor < previousCursor && !nextCursor.startsWith('plane=');
}
