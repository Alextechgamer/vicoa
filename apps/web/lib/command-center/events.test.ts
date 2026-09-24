import { describe, expect, it } from 'vitest';

import { applyEvents } from './events';

describe('command center events', () => {
  it('applies only unseen events and keeps order', () => {
    const current = [{ id: 'legacy:1', event_type: 'legacy', message: 'created', created_at: '1' }];
    const incoming = [
      { id: 'legacy:1', event_type: 'legacy', message: 'created', created_at: '1' },
      { id: 'plane:2', event_type: 'handoff_started', message: 'HANDOFF_STARTED', created_at: '2' },
    ];
    const next = applyEvents(current, incoming);
    expect(next.map((item) => item.id)).toEqual(['legacy:1', 'plane:2']);
  });
});
