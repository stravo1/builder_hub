from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from builder_hub.extensions.submission import _verify_connected_github_account


class PermissionClient:
	def __init__(self, permission):
		self.permission = permission

	def get_repository_permission(self, repository, username):
		return self.permission


class SubmissionOwnershipTests(UnitTestCase):
	@patch("builder_hub.extensions.submission.frappe.db.get_value")
	def test_personal_repository_requires_matching_connected_account_id(self, get_value):
		get_value.return_value = frappe._dict(userid="123", username="author")
		repository = {"owner": {"id": 123, "type": "User"}}
		_verify_connected_github_account(PermissionClient({}), repository)
		repository["owner"]["id"] = 456
		with self.assertRaises(frappe.ValidationError):
			_verify_connected_github_account(PermissionClient({}), repository)

	@patch("builder_hub.extensions.submission.frappe.db.get_value")
	def test_organization_repository_requires_write_permission_for_same_account(self, get_value):
		get_value.return_value = frappe._dict(userid="123", username="author")
		repository = {"owner": {"id": 999, "type": "Organization"}}
		client = PermissionClient({"user": {"id": 123}, "role_name": "maintain"})
		_verify_connected_github_account(client, repository)
		client.permission = {"user": {"id": 123}, "role_name": "read"}
		with self.assertRaises(frappe.ValidationError):
			_verify_connected_github_account(client, repository)

	@patch("builder_hub.extensions.submission.frappe.db.get_value", return_value=None)
	def test_submission_requires_connected_github_account(self, _get_value):
		with self.assertRaises(frappe.ValidationError):
			_verify_connected_github_account(PermissionClient({}), {"owner": {"type": "User"}})
