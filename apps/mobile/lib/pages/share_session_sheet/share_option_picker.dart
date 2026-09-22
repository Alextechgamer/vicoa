import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:google_fonts/google_fonts.dart';

import '/flutter_flow/flutter_flow_theme.dart';

/// One choice in [showShareOptionPicker].
class ShareOption<T> {
  const ShareOption({required this.value, required this.label, this.icon});

  final T value;
  final String label;
  final IconData? icon;
}

/// A one-column picker for a share-link setting (who can open it, when it
/// expires). Returns the chosen value, or null when dismissed.
///
/// A dropdown would be the web's answer; on a phone the list *is* the control
/// — every option is a tap target and the current one is ticked, so picking
/// never needs a second confirmation step.
Future<T?> showShareOptionPicker<T>(
  BuildContext context, {
  required String title,
  required List<ShareOption<T>> options,
  required T current,
}) {
  return showModalBottomSheet<T>(
    context: context,
    isScrollControlled: true,
    backgroundColor: Colors.transparent,
    builder: (sheetContext) {
      final theme = FlutterFlowTheme.of(sheetContext);
      return Container(
        width: double.infinity,
        decoration: BoxDecoration(
          color: theme.secondaryBackground,
          borderRadius: const BorderRadius.only(topLeft: Radius.circular(24.0), topRight: Radius.circular(24.0)),
        ),
        child: SafeArea(
          top: false,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Padding(
                padding: const EdgeInsetsDirectional.fromSTEB(0.0, 12.0, 0.0, 0.0),
                child: Center(
                  child: Container(
                    width: 50.0,
                    height: 4.0,
                    decoration: BoxDecoration(color: theme.alternate, borderRadius: BorderRadius.circular(8.0)),
                  ),
                ),
              ),
              Padding(
                padding: const EdgeInsetsDirectional.fromSTEB(18.0, 16.0, 18.0, 8.0),
                child: Text(
                  title,
                  style: theme.bodyMedium.override(font: GoogleFonts.sourceSans3(), fontSize: 20.0, letterSpacing: 0.0),
                ),
              ),
              Padding(
                padding: const EdgeInsetsDirectional.fromSTEB(16.0, 8.0, 16.0, 24.0),
                child: Container(
                  width: double.infinity,
                  clipBehavior: Clip.antiAlias,
                  decoration: BoxDecoration(color: theme.primaryBackground, borderRadius: BorderRadius.circular(14.0)),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      for (var i = 0; i < options.length; i++) ...[
                        if (i > 0)
                          Padding(
                            padding: const EdgeInsetsDirectional.fromSTEB(16.0, 0.0, 0.0, 0.0),
                            child: Container(height: 0.5, color: theme.secondaryText.withValues(alpha: 0.12)),
                          ),
                        Material(
                          color: Colors.transparent,
                          child: InkWell(
                            onTap: () {
                              HapticFeedback.lightImpact();
                              Navigator.pop(sheetContext, options[i].value);
                            },
                            child: Padding(
                              padding: const EdgeInsetsDirectional.fromSTEB(16.0, 14.0, 16.0, 14.0),
                              child: Row(
                                children: [
                                  if (options[i].icon != null)
                                    Padding(
                                      padding: const EdgeInsetsDirectional.fromSTEB(0.0, 0.0, 12.0, 0.0),
                                      child: Icon(options[i].icon, size: 18.0, color: theme.secondaryText),
                                    ),
                                  Expanded(
                                    child: Text(
                                      options[i].label,
                                      style: theme.bodyMedium.override(
                                        font: GoogleFonts.sourceSans3(),
                                        fontSize: 16.0,
                                        color: theme.primaryText,
                                        letterSpacing: 0.0,
                                      ),
                                    ),
                                  ),
                                  if (options[i].value == current)
                                    Icon(Icons.check_rounded, size: 18.0, color: theme.primaryText),
                                ],
                              ),
                            ),
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
      );
    },
  );
}
