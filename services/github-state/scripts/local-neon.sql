-- Roles and database matching the isolated Neon test branch (POSTGRES.md, "Provisioning and
-- credentials"), on a disposable local PostgreSQL. Like Neon's console-created roles, the
-- migration role can create databases, end other sessions and act as the application role;
-- the application role has no extra privileges.
CREATE ROLE github_state_test_migration LOGIN PASSWORD 'migration' CREATEDB;
GRANT pg_signal_backend TO github_state_test_migration;
CREATE ROLE github_state_test_app LOGIN PASSWORD 'app';
GRANT github_state_test_app TO github_state_test_migration;
CREATE DATABASE github_state_test OWNER github_state_test_migration;
-- Neon's proxy, in its local PostgreSQL authentication mode, reads endpoint IP allow lists here.
CREATE SCHEMA neon_control_plane;
CREATE TABLE neon_control_plane.endpoints (endpoint_id VARCHAR(255) PRIMARY KEY, allowed_ips VARCHAR(255));
