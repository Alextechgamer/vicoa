import 'package:flutter/material.dart';

import '/flutter_flow/flutter_flow_util.dart';
import 'share_session_sheet_widget.dart' show ShareSessionSheetWidget;

/// Holds the sheet's data so the widget only draws it: the live links on this
/// session, plus the in-flight flags the UI disables itself on.
class ShareSessionSheetModel extends FlutterFlowModel<ShareSessionSheetWidget> {
  /// Live links, newest first. Null while the first load is running — "no
  /// links yet" and "we don't know yet" draw differently.
  List<Map<String, dynamic>>? links;

  /// The load failed. Distinct from an empty list: the sheet must not offer to
  /// mint a second link when it could not see the first one.
  bool loadFailed = false;

  bool creating = false;

  /// A settings write or a revoke is in flight. One flag for the whole
  /// section, so two quick taps cannot race two PATCHes onto one link.
  bool busy = false;

  bool settingsOpen = false;
  bool othersOpen = false;

  Map<String, dynamic>? get current =>
      (links != null && links!.isNotEmpty) ? links!.first : null;

  List<Map<String, dynamic>> get others =>
      (links == null || links!.length < 2) ? const [] : links!.sublist(1);

  @override
  void initState(BuildContext context) {}

  @override
  void dispose() {}
}
