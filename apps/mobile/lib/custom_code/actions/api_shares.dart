import 'package:flutter/foundation.dart';

import 'index.dart';

/// Share links for one session — `/api/v1/shares` (collaboration §3.4).
///
/// A share link is a different thing from the export flow next to it in the
/// share sheet: it is live (a visitor keeps seeing what the session does next),
/// it is revocable, and it is a URL rather than a copy of the transcript taken
/// at one moment.
///
/// Every call here is owner-side and needs the signed-in user's token, which
/// [vicoaApiRequest] attaches. The *public* half of the API
/// (`/api/v1/public/shares/<token>/…`) is what a visitor's browser talks to;
/// the app never calls it.

/// The site a link is opened on.
///
/// Mirrors [getVicoaApiBaseUrl]'s debug/release split: release always points
/// at production, and debug points wherever the backend you are testing
/// against publishes its pages — flip the local line when running the
/// dashboard locally (the Android emulator reaches the host as 10.0.2.2), so
/// a token minted by a local backend opens on a page that can resolve it.
String getVicoaWebBaseUrl() {
  if (kDebugMode) {
    // return 'http://localhost:3000';
    return 'https://vicoa.ai';
  }
  return 'https://vicoa.ai';
}

/// The public URL of a link, from the token the API returned.
String shareLinkUrl(String token) => '${getVicoaWebBaseUrl()}/share/$token';

/// GET /api/v1/shares?agent_instance_id=… — the live (unrevoked) links on one
/// session, newest first.
///
/// Errors are rethrown rather than swallowed into an empty list: "this session
/// has no link" and "we could not find out" are different sentences for the
/// sheet to say, and the second one must not offer to mint a second link.
Future<List<dynamic>> apiListShareLinks(String instanceId) async {
  try {
    final result = await vicoaApiRequest(
      'get',
      '/api/v1/shares?agent_instance_id=$instanceId',
      null,
    );
    if (result is List) return result;
    return [];
  } catch (e) {
    debugPrint('Error listing share links: $e');
    rethrow;
  }
}

/// POST /api/v1/shares — mint a link on one session.
///
/// The defaults are the minimal ones the web dialog also starts from: anyone
/// with the link, no expiry, the owner's name and the branch names hidden.
/// Comments are a project-board feature and are never sent from here.
Future<Map<String, dynamic>?> apiCreateSessionShareLink(
  String instanceId, {
  String audience = 'public',
  bool showOwner = false,
  bool showBranch = false,
  int? expiresInDays,
}) async {
  try {
    final result = await vicoaApiRequest('post', '/api/v1/shares', {
      'kind': 'session',
      'agent_instance_id': instanceId,
      'audience': audience,
      'show_owner': showOwner,
      'show_branch': showBranch,
      'expires_in_days': expiresInDays,
    });
    if (result is Map) return Map<String, dynamic>.from(result);
    return null;
  } catch (e) {
    debugPrint('Error creating share link: $e');
    return null;
  }
}

/// PATCH /api/v1/shares/{id} — edit a link's settings in place.
///
/// The token does not change, which is the entire reason this is not a
/// create: whoever already has the URL keeps it, and simply sees the new
/// settings. Omitted fields are left alone by the server, so pass only what
/// changed — except [expiresInDays], where `null` is a real value ("never
/// expires") and [clearExpiry] is how you ask for it.
Future<Map<String, dynamic>?> apiUpdateShareLink(
  String linkId, {
  String? audience,
  bool? showOwner,
  bool? showBranch,
  int? expiresInDays,
  bool clearExpiry = false,
}) async {
  final body = <String, dynamic>{};
  if (audience != null) body['audience'] = audience;
  if (showOwner != null) body['show_owner'] = showOwner;
  if (showBranch != null) body['show_branch'] = showBranch;
  if (expiresInDays != null) {
    body['expires_in_days'] = expiresInDays;
  } else if (clearExpiry) {
    body['expires_in_days'] = null;
  }
  if (body.isEmpty) return null;
  try {
    final result =
        await vicoaApiRequest('patch', '/api/v1/shares/$linkId', body);
    if (result is Map) return Map<String, dynamic>.from(result);
    return null;
  } catch (e) {
    debugPrint('Error updating share link: $e');
    return null;
  }
}

/// DELETE /api/v1/shares/{id} — revoke. Everyone holding the URL loses access
/// immediately; the row survives for audit.
///
/// The endpoint answers 204 No Content and the shared request helper treats an
/// empty body as an error, so a *successful* revoke lands in the catch below
/// (same shape as [apiDeleteTask]).
Future<bool> apiRevokeShareLink(String linkId) async {
  try {
    await vicoaApiRequest('delete', '/api/v1/shares/$linkId', null);
    return true;
  } catch (e) {
    if (e.toString().contains('Empty response')) return true;
    debugPrint('Error revoking share link: $e');
    return false;
  }
}
