import 'package:flutter_test/flutter_test.dart';
import 'package:vicoa/custom_code/actions/api_shares.dart';

void main() {
  group('shareLinkUrl', () {
    test('points at the public viewer page on the web host', () {
      expect(shareLinkUrl('abc123'), '${getVicoaWebBaseUrl()}/share/abc123');
    });

    test('is the web host, not the API host', () {
      // A link is opened in a browser, so it must never carry the api.
      // subdomain the app itself talks to — that host serves no page.
      expect(getVicoaWebBaseUrl(), isNot(contains('api.')));
      expect(shareLinkUrl('t'), startsWith('https://'));
    });

    test('an empty token still produces a well-formed path', () {
      // The caller reads the token out of a JSON map; a missing one must not
      // silently become "…/share" pointing at something else.
      expect(shareLinkUrl(''), endsWith('/share/'));
    });
  });
}
