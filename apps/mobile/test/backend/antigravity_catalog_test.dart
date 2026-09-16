import 'package:flutter_test/flutter_test.dart';

import 'package:vicoa/backend/agent_catalog.dart';
import 'package:vicoa/components/agent_type_icon/agent_type_icon_widget.dart';
import 'package:vicoa/custom_code/actions/api_resume_session.dart';

void main() {
  group('antigravity catalog entry', () {
    test('is in the fallback catalog with the four launch-flag modes', () {
      final catalog = agentCatalogFallback();
      final agent = catalog.agentById('antigravity');
      expect(agent?.label, 'Antigravity');
      // default -> request-review, acceptEdits -> --mode accept-edits,
      // plan -> --mode plan, bypassPermissions -> --dangerously-skip-permissions.
      expect(
        agent!.permissionModes.map((m) => m.id).toList(),
        <String>['default', 'acceptEdits', 'plan', 'bypassPermissions'],
      );
      expect(SessionConfig.defaultsFor(catalog, 'antigravity').permissionMode, 'default');
    });

    test('has no thinking picker: effort is baked into every agy model id', () {
      final catalog = agentCatalogFallback();
      final agent = catalog.agentById('antigravity')!;
      expect(agent.thinkingEfforts, isEmpty);
      expect(agent.models!.any((m) => m.id == 'gemini-3.8-flash-high'), isTrue);
      expect(SessionConfig.defaultsFor(catalog, 'antigravity').model, 'default');
    });

    test('toSpawnMetadata passes model + permission mode, never the sentinel', () {
      expect(
        SessionConfig(agent: 'antigravity', model: 'claude-sonnet-4-6', permissionMode: 'acceptEdits')
            .toSpawnMetadata(),
        {'model': 'claude-sonnet-4-6', 'permission_mode': 'acceptEdits'},
      );
      expect(SessionConfig(agent: 'antigravity', model: 'default').toSpawnMetadata(), {});
    });

    test('has a brand mark', () {
      expect(agentTypeHasLogo('Antigravity'), isTrue);
    });
  });

  group('resuming an antigravity session', () {
    test('resolves the slug from the display name', () {
      expect(resumeAgentSlug('Antigravity'), 'antigravity');
    });

    test('carries the agy conversation id into the relaunch', () {
      expect(
        resumeAgentSessionHandle({'antigravity_conversation_id': 'c9065f92-851f'}),
        'c9065f92-851f',
      );
    });
  });
}
