PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Singleton runtime values such as the Telegram update offset.
CREATE TABLE IF NOT EXISTS runtime_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Someone Zoe knows. An owner has full access; a member gets conversation, the web,
-- and reading of their own folder and of the folders the owner shares (folders, JSON).
CREATE TABLE IF NOT EXISTS persons (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('owner', 'member')),
    language   TEXT NOT NULL,
    folders    TEXT,
    created_at REAL NOT NULL
);

-- A Telegram identity. Approved accounts belong to a person, and one person may
-- use several accounts. An unknown account that wrote is kept, unanswered, as
-- pending, with the code the owner's menu buttons use to admit or block it.
CREATE TABLE IF NOT EXISTS accounts (
    telegram_user_id INTEGER PRIMARY KEY,
    person_id        INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    status           TEXT NOT NULL CHECK (status IN ('approved', 'pending', 'blocked')),
    username         TEXT,
    display_name     TEXT,
    language_code    TEXT,
    request_code     TEXT UNIQUE,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_person ON accounts(person_id);

-- Durable log of every inbound and outbound message; the inbound rows double
-- as the work queue through their state column.
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id   INTEGER UNIQUE,
    chat_id     INTEGER NOT NULL,
    person_id   INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    sender_id   INTEGER,
    telegram_id INTEGER,
    direction   TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    kind        TEXT NOT NULL DEFAULT 'text',
    text        TEXT,
    payload     TEXT,
    state       TEXT NOT NULL DEFAULT 'received'
                CHECK (state IN ('received', 'processing', 'answered', 'failed', 'skipped')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_queue ON messages(direction, state, id);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, state, id);
CREATE INDEX IF NOT EXISTS idx_messages_telegram ON messages(chat_id, telegram_id);

-- A question Zoe asked with one button per option, and the option tapped.
CREATE TABLE IF NOT EXISTS choices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    person_id   INTEGER REFERENCES persons(id) ON DELETE CASCADE,
    telegram_id INTEGER,
    options     TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'answered')),
    answer      TEXT,
    created_at  REAL NOT NULL
);

-- Each neuron is one unit of knowledge: a fact, a preference, an event, a word.
-- owner_id is the person the neuron is private to; NULL marks shared knowledge.
CREATE TABLE IF NOT EXISTS neurons (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER REFERENCES persons(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    content       TEXT NOT NULL,
    summary       TEXT,
    salience      REAL NOT NULL DEFAULT 1.0,
    activation    REAL NOT NULL DEFAULT 0.0,
    created_at    REAL NOT NULL,
    last_fired_at REAL,
    fire_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_neurons_scope ON neurons(owner_id, kind, content);
CREATE INDEX IF NOT EXISTS idx_neurons_salience ON neurons(salience DESC);

-- Full-text index of what neurons hold, kept in step by triggers; recall starts from it.
CREATE VIRTUAL TABLE IF NOT EXISTS neurons_fts USING fts5(
    content, summary, content='neurons', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS neurons_fts_insert AFTER INSERT ON neurons BEGIN
    INSERT INTO neurons_fts (rowid, content, summary) VALUES (new.id, new.content, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS neurons_fts_delete AFTER DELETE ON neurons BEGIN
    INSERT INTO neurons_fts (neurons_fts, rowid, content, summary) VALUES ('delete', old.id, old.content, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS neurons_fts_update AFTER UPDATE OF content, summary ON neurons BEGIN
    INSERT INTO neurons_fts (neurons_fts, rowid, content, summary) VALUES ('delete', old.id, old.content, old.summary);
    INSERT INTO neurons_fts (rowid, content, summary) VALUES (new.id, new.content, new.summary);
END;

-- Where each neuron sits by meaning, as the named model places it; a neuron that changes is placed again.
CREATE TABLE IF NOT EXISTS neuron_vectors (
    neuron_id INTEGER PRIMARY KEY REFERENCES neurons(id) ON DELETE CASCADE,
    model     TEXT NOT NULL,
    vector    BLOB NOT NULL
);
CREATE TRIGGER IF NOT EXISTS neuron_vectors_stale AFTER UPDATE OF content ON neurons BEGIN
    DELETE FROM neuron_vectors WHERE neuron_id = old.id;
END;

-- What the nightly review folded into what: a neuron merged away still leads to the one that kept it.
CREATE TABLE IF NOT EXISTS merged (
    gone_id   INTEGER PRIMARY KEY,
    kept_id   INTEGER NOT NULL,
    merged_at REAL NOT NULL
);

-- Weighted directed links between neurons; strengthened when neurons fire together.
CREATE TABLE IF NOT EXISTS connections (
    source_id  INTEGER NOT NULL REFERENCES neurons(id) ON DELETE CASCADE,
    target_id  INTEGER NOT NULL REFERENCES neurons(id) ON DELETE CASCADE,
    weight     REAL NOT NULL DEFAULT 0.1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (source_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_connections_target ON connections(target_id);

-- When a plan neuron comes due, and its cron rule when it repeats.
CREATE TABLE IF NOT EXISTS plans (
    neuron_id INTEGER PRIMARY KEY REFERENCES neurons(id) ON DELETE CASCADE,
    chat_id   INTEGER NOT NULL,
    due_at    REAL NOT NULL,
    repeat    TEXT
);
CREATE INDEX IF NOT EXISTS idx_plans_due ON plans(due_at);

-- Measurements about a person over time: mood seen by the observer, and later
-- readings from devices. Meaning lives in neurons; numbers over time live here.
CREATE TABLE IF NOT EXISTS signals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,
    value     REAL NOT NULL,
    note      TEXT,
    source    TEXT NOT NULL,
    at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_person ON signals(person_id, kind, at);

-- Messages programs on the machine ask Zoe to send (zoe-say). The service delivers
-- them and logs them in the chat, so Zoe knows what was said in her name.
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    text       TEXT,
    file       TEXT,
    kind       TEXT,
    origin     TEXT,
    silent     INTEGER NOT NULL DEFAULT 0,
    state      TEXT NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting', 'sent', 'failed')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_state ON outbox(state, id);

-- Files people sent. A file is temporary until Zoe keeps it: a temporary one is
-- deleted at expires_at; a kept one has no expiry and a neuron that remembers
-- what it is. path follows the file when Zoe moves it; telegram_id lets it be
-- sent back without uploading it again.
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    neuron_id   INTEGER REFERENCES neurons(id) ON DELETE SET NULL,
    person_id   INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    path        TEXT NOT NULL,
    name        TEXT NOT NULL,
    telegram_id TEXT,
    size        INTEGER,
    expires_at  REAL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_expiry ON files(expires_at);

-- Long work Zoe handed to a parallel mind. session_id lets a stopped run resume
-- where it was; notes hold what the person added while it worked.
CREATE TABLE IF NOT EXISTS minds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    neuron_id   INTEGER REFERENCES neurons(id) ON DELETE SET NULL,
    person_id   INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    chat_id     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    brief       TEXT NOT NULL,
    workdir     TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued', 'running', 'asking', 'done', 'failed', 'stopped')),
    session_id  TEXT,
    directive_key TEXT,
    steps       TEXT,
    notes       TEXT,
    question    TEXT,
    result      TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    started_at  REAL,
    finished_at REAL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_minds_state ON minds(state, id);

-- Whether the observer learns about a person, and the last message it read.
-- A person without a row is observed when they are an owner.
CREATE TABLE IF NOT EXISTS observation (
    person_id INTEGER PRIMARY KEY REFERENCES persons(id) ON DELETE CASCADE,
    enabled   INTEGER NOT NULL,
    cursor    INTEGER NOT NULL DEFAULT 0
);

-- What the nightly study proposes to improve (improve.py): waiting for the owner,
-- approved and handed to a parallel mind, or set aside so it is not proposed again.
CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    benefit     TEXT NOT NULL,
    plan        TEXT NOT NULL,
    checked     TEXT NOT NULL DEFAULT '',
    careful     INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting', 'approved', 'rejected')),
    mind_id     INTEGER REFERENCES minds(id) ON DELETE SET NULL,
    created_at  REAL NOT NULL,
    decided_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_proposals_state ON proposals(state, id);

-- Actions waiting for an owner's yes on Telegram (approve.py): the hook that asked
-- waits for the state to change; settled once the message shows the outcome.
CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    asker       TEXT NOT NULL DEFAULT '',
    tool        TEXT NOT NULL,
    args        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting', 'allowed', 'denied', 'expired')),
    message_id  INTEGER,
    settled     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    answered_at REAL
);
CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state, id);
