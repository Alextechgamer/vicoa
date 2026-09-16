import { describe, expect, test } from 'vitest';
import {
  AGENT_CATALOG_FALLBACK,
  agentById,
  defaultsFor,
  toSpawnMetadata,
} from './agent-catalog';
import { getAgentLogoSrc } from '@/components/dashboard/agent-type-icon';
import { agentSessionHandle, resumeAgentSlug } from './session-resume';

describe('antigravity catalog entry', () => {
  test('is in the fallback catalog with the four launch-flag permission modes', () => {
    const agent = agentById(AGENT_CATALOG_FALLBACK, 'antigravity');
    expect(agent?.label).toBe('Antigravity');
    // default -> request-review, acceptEdits -> --mode accept-edits,
    // plan -> --mode plan, bypassPermissions -> --dangerously-skip-permissions.
    expect(agent?.permission_modes?.map((m) => m.id)).toEqual([
      'default',
      'acceptEdits',
      'plan',
      'bypassPermissions',
    ]);
    expect(defaultsFor(AGENT_CATALOG_FALLBACK, 'antigravity').permission_mode).toBe('default');
  });

  test('has no thinking picker: effort is baked into every agy model id', () => {
    // `--effort` conflicts with a suffixed Gemini id and is rejected outright
    // by the Claude ids, so the model picker IS the effort picker.
    const agent = agentById(AGENT_CATALOG_FALLBACK, 'antigravity');
    expect(agent?.thinking_efforts).toBeUndefined();
    expect(agent?.models?.some((m) => m.id === 'gemini-3.8-flash-high')).toBe(true);
    expect(agent?.models?.some((m) => m.id === 'gemini-3.8-flash-low')).toBe(true);
  });

  test('starts on the "keep agy\'s own model" sentinel and does not send it', () => {
    expect(defaultsFor(AGENT_CATALOG_FALLBACK, 'antigravity').model).toBe('default');
    expect(toSpawnMetadata({ agent: 'antigravity', model: 'default', permission_mode: 'default' })).toEqual({
      permission_mode: 'default',
    });
  });

  test('sends an explicit model and permission mode', () => {
    expect(
      toSpawnMetadata({
        agent: 'antigravity',
        model: 'claude-sonnet-4-6',
        permission_mode: 'acceptEdits',
      })
    ).toEqual({ model: 'claude-sonnet-4-6', permission_mode: 'acceptEdits' });
  });

  test('does not offer steer', () => {
    expect(agentById(AGENT_CATALOG_FALLBACK, 'antigravity')?.supports_steer).toBeFalsy();
  });

  test('has a brand mark', () => {
    expect(getAgentLogoSrc('Antigravity')?.src).toBe('/images/integrations/antigravity.svg');
    expect(getAgentLogoSrc('antigravity')?.alt).toBe('Antigravity');
  });
});

describe('resuming an antigravity session', () => {
  test('resolves the slug from the display name when session_config is absent', () => {
    expect(
      resumeAgentSlug({ id: 'x', status: 'COMPLETED', agent_type_name: 'Antigravity' })
    ).toBe('antigravity');
  });

  test('carries the agy conversation id into the relaunch', () => {
    expect(
      agentSessionHandle({
        id: 'x',
        status: 'COMPLETED',
        instance_metadata: { antigravity_conversation_id: 'c9065f92-851f-447c' },
      })
    ).toBe('c9065f92-851f-447c');
  });
});
