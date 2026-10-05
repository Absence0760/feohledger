import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/user.dart';

User _user(List<String> roles) => User(
      id: 'u1',
      email: 'a@acme.com',
      fullName: 'Ada Clerk',
      organizationId: 'org1',
      roles: roles,
    );

void main() {
  group('User.fromJson', () {
    test('parses all fields and casts roles to List<String>', () {
      final user = User.fromJson({
        'id': 'u1',
        'email': 'a@acme.com',
        'full_name': 'Ada Lovelace',
        'organization_id': 'org1',
        'roles': <dynamic>['admin', 'cfo'],
      });

      expect(user.id, 'u1');
      expect(user.email, 'a@acme.com');
      expect(user.fullName, 'Ada Lovelace');
      expect(user.organizationId, 'org1');
      expect(user.roles, ['admin', 'cfo']);
    });

    test('handles an empty roles list', () {
      final user = User.fromJson({
        'id': 'u1',
        'email': 'a@acme.com',
        'full_name': 'Nobody',
        'organization_id': 'org1',
        'roles': <dynamic>[],
      });
      expect(user.roles, isEmpty);
      expect(user.isAdmin, isFalse);
      expect(user.isClerkOnly, isFalse);
    });

    test('parses the effective permissions /auth/me returns', () {
      final user = User.fromJson({
        'id': 'u1',
        'email': 'a@acme.com',
        'full_name': 'Ada',
        'organization_id': 'org1',
        'roles': <dynamic>['cfo'],
        'permissions': <dynamic>['invoice.approve', 'payment.void'],
      });
      expect(user.permissions, ['invoice.approve', 'payment.void']);
      expect(user.can(kPermInvoiceApprove), isTrue);
      expect(user.can('user.manage'), isFalse);
    });

    test('a payload without permissions grants none (fail closed)', () {
      // Never re-derived from the role names: an admin is not assumed to hold
      // invoice.approve unless the server says so.
      final user = User.fromJson({
        'id': 'u1',
        'email': 'a@acme.com',
        'full_name': 'Ada',
        'organization_id': 'org1',
        'roles': <dynamic>['admin'],
      });
      expect(user.permissions, isEmpty);
      expect(user.can(kPermInvoiceApprove), isFalse);
    });
  });

  group('role helpers', () {
    test('hasRole reflects membership', () {
      final user = _user(['ap_manager']);
      expect(user.hasRole('ap_manager'), isTrue);
      expect(user.hasRole('admin'), isFalse);
    });

    test('isAdmin / isManager / isCfo map to backend role slugs', () {
      expect(_user(['admin']).isAdmin, isTrue);
      expect(_user(['ap_manager']).isManager, isTrue);
      expect(_user(['cfo']).isCfo, isTrue);

      expect(_user(['ap_clerk']).isAdmin, isFalse);
      expect(_user(['ap_clerk']).isManager, isFalse);
      expect(_user(['ap_clerk']).isCfo, isFalse);
    });

    test('isClerkOnly is true only for a sole ap_clerk role', () {
      expect(_user(['ap_clerk']).isClerkOnly, isTrue);
      // A clerk who is also a manager is not "clerk only".
      expect(_user(['ap_clerk', 'ap_manager']).isClerkOnly, isFalse);
      // Some other single role is not clerk-only.
      expect(_user(['cfo']).isClerkOnly, isFalse);
      expect(_user([]).isClerkOnly, isFalse);
    });

    test('multiple roles are all recognized', () {
      final user = _user(['admin', 'cfo']);
      expect(user.isAdmin, isTrue);
      expect(user.isCfo, isTrue);
      expect(user.isManager, isFalse);
    });
  });
}
