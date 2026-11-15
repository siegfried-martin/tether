-- OAuth for the MCP endpoint (spec: "MCP auth"). Tether is the authorization server.
-- Tokens are stored only as SHA-256 hashes.

-- Clients registered through dynamic client registration (RFC 7591), e.g. Claude's connector.
CREATE TABLE oauth_client (
    client_id   TEXT PRIMARY KEY,
    info        TEXT NOT NULL,           -- OAuthClientInformationFull as JSON
    created_at  TEXT NOT NULL
);

-- One row per approval on the consent page. Revoking a grant kills all its tokens.
CREATE TABLE oauth_grant (
    id          INTEGER PRIMARY KEY,
    client_id   TEXT NOT NULL REFERENCES oauth_client(client_id),
    subject     TEXT NOT NULL,
    scopes      TEXT NOT NULL DEFAULT '',
    resource    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
);

CREATE TABLE oauth_token (
    token_hash  TEXT PRIMARY KEY,
    grant_id    INTEGER NOT NULL REFERENCES oauth_grant(id),
    kind        TEXT NOT NULL CHECK (kind IN ('code', 'access', 'refresh')),
    expires_at  INTEGER NOT NULL,        -- unix seconds
    data        TEXT,                    -- code only: PKCE challenge and redirect URI, as JSON
    created_at  TEXT NOT NULL
);
CREATE INDEX oauth_token_grant ON oauth_token (grant_id);
