CREATE TABLE activity_deliveries (
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    activity_seq INTEGER NOT NULL REFERENCES activity_items(seq),
    activity_key TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    input_run_id TEXT REFERENCES dispatch_jobs(job_id),
    input_index INTEGER CHECK(input_index>=0),
    PRIMARY KEY(conversation_id,activity_seq)
);

CREATE INDEX activity_baseline ON activity_deliveries(conversation_id,activity_key,activity_seq);

CREATE INDEX activity_input_slot ON activity_deliveries(input_run_id,input_index);

CREATE INDEX activity_pending ON activity_deliveries(conversation_id,activity_seq) WHERE applied=0;

CREATE TABLE activity_heads (
    user_id TEXT NOT NULL,
    activity_key TEXT NOT NULL,
    source_seq INTEGER,
    occurred_at REAL NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY(user_id,activity_key)
);

CREATE TABLE activity_items (
    seq INTEGER PRIMARY KEY,
    activity_id TEXT UNIQUE NOT NULL,
    after_receipt INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    activity_key TEXT NOT NULL,
    occurred_at REAL NOT NULL,
    content TEXT NOT NULL,
    data TEXT NOT NULL,
    canonical_change INTEGER NOT NULL
);

CREATE INDEX activity_timeline ON activity_items(user_id,occurred_at,seq);

CREATE TABLE audio_transcripts (
    event_id TEXT PRIMARY KEY REFERENCES slice_events(event_id),
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    user_id TEXT,
    conversation_id TEXT
);

CREATE INDEX transcript_history ON audio_transcripts(user_id,conversation_id,CAST(ROUND(created_at*1000) AS INTEGER));

CREATE TABLE chat_documents (
    user_id TEXT,
    conversation_id TEXT,
    id TEXT,
    raw BLOB,
    markdown TEXT,
    owner TEXT,
    expires REAL,
    PRIMARY KEY(user_id,conversation_id,id)
);

CREATE TABLE chat_images (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES conversation_events(event_id) ON DELETE CASCADE,
    data BLOB NOT NULL
);

CREATE TABLE client_devices (
    device_id TEXT PRIMARY KEY REFERENCES devices,
    installation TEXT,
    name TEXT,
    platform TEXT
);

CREATE TABLE connector_calls (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    connector_name TEXT NOT NULL,
    tool_type TEXT NOT NULL CHECK(tool_type IN ('mcp','device','cli')),
    target TEXT NOT NULL DEFAULT 'default',
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    output TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    completed_at REAL
);

CREATE INDEX connector_calls_history ON connector_calls(user_id, conversation_id, created_at DESC, seq DESC);

CREATE TABLE connector_credentials (
    user_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    connection_id TEXT NOT NULL DEFAULT 'default',
    label TEXT NOT NULL DEFAULT '默认账号',
    values_json TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(user_id, connector_id, connection_id),
    FOREIGN KEY(user_id, connector_id) REFERENCES connectors(user_id, id) ON DELETE CASCADE
);

CREATE TABLE connectors (
    user_id TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    tool_type TEXT NOT NULL CHECK(tool_type IN ('mcp','device','cli')),
    endpoint TEXT,
    source_kind TEXT,
    source_found_at TEXT,
    source_publisher TEXT,
    header_names TEXT NOT NULL DEFAULT '[]',
    credential_label TEXT,
    credential_query_param TEXT,
    auth_mode TEXT NOT NULL DEFAULT 'none' CHECK(auth_mode IN ('none','credential','oauth','device')),
    scopes TEXT NOT NULL DEFAULT '[]',
    grants TEXT NOT NULL DEFAULT '{}',
    device_id TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    verified INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(user_id, id)
);

CREATE TABLE conversation_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL REFERENCES receipts(event_id),
    user_id TEXT NOT NULL,
    conversation_id TEXT,
    data TEXT NOT NULL
);

CREATE INDEX conversation_events_scope ON conversation_events(user_id,conversation_id,seq);

CREATE INDEX conversation_history_time ON conversation_events(user_id,conversation_id,CAST(ROUND((julianday(json_extract(data,'$.occurred_at'))-2440587.5)*86400000) AS INTEGER),seq);

CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL
);

CREATE TABLE desktop_frame_uploads (
    user_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    producer_event_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY(device_id,producer_event_id)
);

CREATE TABLE desktop_vision_queue (
    event_id TEXT NOT NULL REFERENCES perception_events(event_id),
    frame_index INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(event_id,frame_index)
);

CREATE INDEX desktop_vision_due ON desktop_vision_queue(retry_at);

CREATE TABLE device_connectors (
    user_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    verified INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(user_id, device_id, id),
    FOREIGN KEY(user_id, id) REFERENCES connectors(user_id, id) ON DELETE CASCADE
);

CREATE TABLE devices (
    device_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE dialogue_entries (
    seq INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    event_id TEXT UNIQUE REFERENCES conversation_events(event_id),
    output_id TEXT UNIQUE REFERENCES outputs(output_id),
    completed_at TEXT,
    CHECK((event_id IS NULL) != (output_id IS NULL))
);

CREATE INDEX dialogue_order ON dialogue_entries(user_id,conversation_id,seq);

CREATE INDEX profile_input_order ON dialogue_entries(user_id,seq) WHERE event_id IS NOT NULL;

CREATE TABLE dispatch_jobs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,
    event_id TEXT UNIQUE NOT NULL REFERENCES receipts(event_id),
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    layer TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    messages TEXT NOT NULL DEFAULT '[]',
    run_id TEXT REFERENCES dispatch_jobs(job_id),
    input_run_id TEXT REFERENCES dispatch_jobs(job_id),
    input_index INTEGER CHECK(input_index >= 0),
    UNIQUE(input_run_id,input_index)
);

CREATE INDEX dispatch_ready ON dispatch_jobs(status,conversation_id,seq);

CREATE INDEX dispatch_run ON dispatch_jobs(run_id,seq);

CREATE INDEX profile_busy ON dispatch_jobs(user_id) WHERE status IN ('queued','running');

CREATE TABLE holiday_cn_years (
    year INTEGER PRIMARY KEY,
    checked_at REAL NOT NULL,
    document BLOB NOT NULL
);

CREATE TABLE http_device_credentials (
    device_id TEXT PRIMARY KEY REFERENCES devices,
    binding TEXT NOT NULL,
    digest BLOB
);

CREATE TABLE memory_schedules (
    user_id TEXT PRIMARY KEY,
    interval_days INTEGER NOT NULL,
    local_time TEXT NOT NULL,
    next_date TEXT NOT NULL,
    timezone TEXT
);

CREATE TABLE merged_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT NOT NULL,
    start INTEGER NOT NULL,
    first_seq INTEGER NOT NULL,
    end INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    size INTEGER NOT NULL,
    tokens INTEGER NOT NULL,
    synopsis TEXT NOT NULL,
    ai TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0,
    generated_at INTEGER
);

CREATE INDEX merged_history_order ON merged_batches(room,COALESCE(generated_at,end),last_seq);

CREATE INDEX merged_intervals ON merged_batches(room,end,last_seq);

CREATE INDEX merged_jobs ON merged_batches(retry_at,id) WHERE ai IS NULL;

CREATE TABLE merged_context_writes (
    room TEXT NOT NULL,
    history_seq INTEGER NOT NULL REFERENCES session_history(seq),
    slot INTEGER NOT NULL,
    at INTEGER NOT NULL,
    original TEXT NOT NULL,
    covered_from TEXT,
    covered_until TEXT,
    PRIMARY KEY(history_seq,slot)
);

CREATE INDEX merged_write_order ON merged_context_writes(room,at,history_seq,slot);

CREATE TABLE merged_entries (
    room TEXT NOT NULL,
    seq INTEGER NOT NULL REFERENCES activity_items(seq),
    at INTEGER NOT NULL,
    layer TEXT NOT NULL,
    slice_key TEXT,
    line TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    batch INTEGER REFERENCES merged_batches(id) ON DELETE SET NULL,
    PRIMARY KEY(room,seq)
);

CREATE INDEX merged_members ON merged_entries(batch,at,seq);

CREATE INDEX merged_order ON merged_entries(room,at,seq);

CREATE UNIQUE INDEX merged_slice ON merged_entries(room,slice_key) WHERE slice_key IS NOT NULL;

CREATE INDEX merged_unbatched ON merged_entries(room,at,seq) WHERE batch IS NULL;

CREATE TABLE mobile_location_queue (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE REFERENCES perception_events(event_id) ON DELETE CASCADE,
    project INTEGER NOT NULL
);

CREATE TABLE notification_deliveries (
    device_id TEXT NOT NULL REFERENCES devices,
    notification_seq INTEGER NOT NULL REFERENCES user_notifications,
    received_at TEXT,
    read_at TEXT,
    PRIMARY KEY(device_id,notification_seq)
);

CREATE INDEX notification_pending ON notification_deliveries(device_id,notification_seq) WHERE received_at IS NULL;

CREATE INDEX notification_target_devices ON notification_deliveries(notification_seq,device_id);

CREATE TABLE notification_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL REFERENCES receipts(event_id),
    user_id TEXT NOT NULL,
    conversation_id TEXT,
    data TEXT NOT NULL
);

CREATE INDEX notification_events_scope ON notification_events(user_id,conversation_id,seq);

CREATE INDEX notification_history_time ON notification_events(user_id,conversation_id,CAST(ROUND((julianday(json_extract(data,'$.occurred_at'))-2440587.5)*86400000) AS INTEGER),seq);

CREATE TABLE notification_files (
    seq INTEGER PRIMARY KEY,
    id TEXT UNIQUE NOT NULL,
    notification_seq INTEGER REFERENCES user_notifications NOT NULL,
    name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    data BLOB NOT NULL
);

CREATE INDEX notification_files_parent ON notification_files(notification_seq);

CREATE TABLE "outputs" (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    output_id TEXT UNIQUE NOT NULL,
    event_id TEXT NOT NULL REFERENCES receipts(event_id),
    job_id TEXT NOT NULL REFERENCES dispatch_jobs(job_id),
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    device_id TEXT,
    layer TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    is_final INTEGER NOT NULL DEFAULT 1 CHECK(is_final IN (0,1))
);

CREATE UNIQUE INDEX final_output_job ON outputs(job_id) WHERE is_final=1;

CREATE INDEX history_output_event ON outputs(event_id);

CREATE INDEX outputs_job ON outputs(job_id);

CREATE TABLE perception_buffers (
    conversation_id TEXT PRIMARY KEY,
    covered INTEGER NOT NULL,
    tail_cost INTEGER NOT NULL,
    previous_id INTEGER,
    job_id INTEGER
);

CREATE TABLE perception_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL REFERENCES receipts(event_id),
    user_id TEXT NOT NULL,
    conversation_id TEXT,
    data TEXT NOT NULL,
    filter_reason TEXT
);

CREATE INDEX mapped_mobile_location ON perception_events(user_id,conversation_id,json_extract(data,'$.device_id'),seq DESC) WHERE json_extract(data,'$.payload.state_key')='mobile.location' AND json_type(data,'$.payload.map_location.address')='text' AND filter_reason IS NULL;

CREATE INDEX perception_events_scope ON perception_events(user_id,conversation_id,seq);

CREATE INDEX perception_history_time ON perception_events(user_id,conversation_id,CAST(ROUND((julianday(json_extract(data,'$.occurred_at'))-2440587.5)*86400000) AS INTEGER),seq);

CREATE TABLE perception_rounds (
    batch_id INTEGER PRIMARY KEY REFERENCES timeline_batches(batch_id),
    conversation_id TEXT NOT NULL,
    after_seq INTEGER NOT NULL,
    through_seq INTEGER NOT NULL,
    consumed INTEGER NOT NULL,
    previous_batch_id INTEGER REFERENCES perception_rounds(batch_id),
    devices TEXT NOT NULL DEFAULT '[]',
    duration_seconds REAL,
    original_summary TEXT
);

CREATE INDEX perception_round_scope ON perception_rounds(conversation_id,through_seq);

CREATE TABLE perception_state (
    user_id TEXT NOT NULL,
    conversation_scope TEXT NOT NULL,
    device_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    state_key TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES receipts(event_id),
    valid_until REAL,
    PRIMARY KEY(user_id,conversation_scope,device_id,source_id,subject,state_key)
);

CREATE TABLE perception_summary_history (
    batch_id INTEGER PRIMARY KEY REFERENCES perception_rounds(batch_id),
    conversation_id TEXT NOT NULL,
    position_ms INTEGER NOT NULL,
    generated_at TEXT
);

CREATE INDEX perception_summary_order ON perception_summary_history(conversation_id,position_ms,batch_id);

CREATE TABLE profile_progress (
    user_id TEXT PRIMARY KEY,
    cursor INTEGER NOT NULL CHECK(cursor>=0)
);

CREATE TABLE receipts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    device_id TEXT NOT NULL,
    producer_event_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    layer TEXT NOT NULL,
    job_id TEXT,
    UNIQUE(device_id,producer_event_id)
);

CREATE TABLE record_batches (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT UNIQUE NOT NULL,
    user_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','processing','completed','partial_failed','failed')),
    total_items INTEGER NOT NULL,
    accepted_items INTEGER NOT NULL DEFAULT 0,
    deduplicated_items INTEGER NOT NULL DEFAULT 0,
    filtered_items INTEGER NOT NULL DEFAULT 0,
    processed_items INTEGER NOT NULL DEFAULT 0,
    failed_items INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX record_history ON record_batches(user_id,seq DESC);

CREATE TABLE record_items (
    item_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES record_batches(batch_id),
    user_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    external_id TEXT,
    content_sha256 TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    title TEXT,
    payload_json TEXT,
    payload_path TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','filtered','processing','processed','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    processed_at TEXT,
    CHECK((payload_json IS NULL)!=(payload_path IS NULL)),
    UNIQUE(user_id,record_type,source_name,dedup_key,content_sha256)
);

CREATE INDEX record_external ON record_items(user_id,record_type,source_name,external_id) WHERE external_id IS NOT NULL;

CREATE INDEX record_pending ON record_items(user_id,status,occurred_at,item_id);

CREATE INDEX record_timeline ON record_items(user_id,occurred_at DESC,item_id DESC);

CREATE INDEX record_type_timeline ON record_items(user_id,record_type,occurred_at DESC,item_id DESC);

CREATE TABLE remote_tool_calls (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    device_id TEXT NOT NULL REFERENCES devices,
    reply_device_id TEXT REFERENCES devices,
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    deadline REAL,
    started_at REAL,
    completed_at REAL,
    claim_hash BLOB,
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    truncated INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id,conversation_id,run_id,call_id)
);

CREATE INDEX remote_tool_deadlines ON remote_tool_calls(deadline) WHERE deadline IS NOT NULL;

CREATE INDEX remote_tool_history ON remote_tool_calls (user_id,conversation_id,seq DESC);

CREATE INDEX remote_tool_queue ON remote_tool_calls (device_id,user_id,conversation_id,seq) WHERE status='queued';

CREATE TABLE scheduled_notification_files (
    seq INTEGER PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES schedules ON DELETE CASCADE,
    name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    data BLOB NOT NULL
);

CREATE INDEX scheduled_files_parent ON scheduled_notification_files(schedule_id);

CREATE TABLE scheduled_notifications (
    id TEXT PRIMARY KEY NOT NULL,
    schedule_id TEXT UNIQUE REFERENCES schedules ON DELETE SET NULL,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    targets TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    UNIQUE(user_id,conversation_id,request_key)
);

CREATE TABLE scheduler_lease (
    slot INTEGER PRIMARY KEY CHECK(slot=1),
    owner_id TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE schedules (
    id TEXT PRIMARY KEY NOT NULL,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    action TEXT NOT NULL,
    trigger_at TEXT NOT NULL,
    triggered_at TEXT
);

CREATE INDEX schedules_calendar ON schedules(user_id,conversation_id,trigger_at,id);

CREATE INDEX schedules_due ON schedules(trigger_at,id) WHERE triggered_at IS NULL;

CREATE TABLE screenshot_frames (
    event_id TEXT PRIMARY KEY REFERENCES slice_events(event_id),
    group_id TEXT NOT NULL REFERENCES screenshot_groups(group_id),
    item_id TEXT REFERENCES slice_stream(item_id),
    occurred_at REAL NOT NULL,
    fact TEXT NOT NULL
);

CREATE INDEX screenshot_frame_group ON screenshot_frames(group_id,occurred_at);

CREATE INDEX screenshot_frame_item ON screenshot_frames(item_id) WHERE item_id IS NOT NULL;

CREATE TABLE screenshot_groups (
    group_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_scope TEXT NOT NULL,
    device_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    app TEXT NOT NULL,
    title TEXT NOT NULL,
    context_key TEXT NOT NULL,
    day TEXT NOT NULL,
    subject TEXT NOT NULL,
    started_at REAL NOT NULL,
    last_at REAL NOT NULL,
    frame_count INTEGER NOT NULL,
    change_count INTEGER NOT NULL,
    latest_fact TEXT NOT NULL,
    last_event TEXT NOT NULL,
    digest TEXT NOT NULL,
    facts TEXT NOT NULL,
    regions TEXT NOT NULL,
    match_digest_only INTEGER NOT NULL
);

CREATE INDEX screenshot_group_history ON screenshot_groups(user_id,conversation_scope,CAST(ROUND(started_at*1000) AS INTEGER));

CREATE TABLE session_history (
    seq INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    run_id TEXT UNIQUE NOT NULL REFERENCES dispatch_jobs(job_id)
);

CREATE INDEX history_order ON session_history(conversation_id,seq);

CREATE TABLE session_model_state (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
    read_count INTEGER NOT NULL CHECK(read_count>=0),
    summary TEXT NOT NULL DEFAULT '',
    summarized_messages INTEGER NOT NULL DEFAULT 0 CHECK(summarized_messages BETWEEN 0 AND read_count),
    prefix_digest TEXT NOT NULL DEFAULT '',
    compactions INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE slice_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL REFERENCES receipts(event_id),
    user_id TEXT NOT NULL,
    conversation_id TEXT,
    data TEXT NOT NULL
);

CREATE INDEX screenshot_preceding ON slice_events(user_id,IFNULL(conversation_id,''),json_extract(data,'$.device_id'),json_extract(data,'$.source_id'),seq) WHERE json_type(data,'$.payload.capture')='object';

CREATE INDEX slice_events_scope ON slice_events(user_id,conversation_id,seq);

CREATE INDEX slice_history_time ON slice_events(user_id,conversation_id,CAST(ROUND((julianday(json_extract(data,'$.occurred_at'))-2440587.5)*86400000) AS INTEGER),seq);

CREATE TABLE slice_processing (
    event_id TEXT PRIMARY KEY REFERENCES slice_events(event_id),
    item_id TEXT REFERENCES slice_stream(item_id),
    attempts INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    retry_at REAL,
    frame_done INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX slice_processing_pending ON slice_processing(item_id,retry_at);

CREATE INDEX slice_waiting ON slice_processing(retry_at,event_id) WHERE item_id IS NULL AND frame_done=0;

CREATE TABLE slice_revisions (
    previous_id TEXT PRIMARY KEY REFERENCES slice_stream(item_id),
    replacement_id TEXT UNIQUE NOT NULL REFERENCES slice_stream(item_id),
    known_at REAL NOT NULL
);

CREATE TABLE slice_stream (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT UNIQUE NOT NULL,
    user_id TEXT NOT NULL,
    conversation_scope TEXT NOT NULL,
    day TEXT NOT NULL,
    timezone TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    created_at REAL NOT NULL,
    data TEXT NOT NULL
);

CREATE INDEX slice_stream_scope ON slice_stream(user_id,day,timezone,conversation_scope,ended_at);

CREATE INDEX slice_summary_history ON slice_stream(user_id,conversation_scope,CAST(ROUND(created_at*1000) AS INTEGER),seq);

CREATE TABLE timeline_batch_members (
    activity_seq INTEGER PRIMARY KEY REFERENCES activity_items(seq),
    batch_id INTEGER NOT NULL REFERENCES timeline_batches(batch_id),
    is_slice INTEGER NOT NULL,
    raw_tokens INTEGER NOT NULL,
    dwell_threshold INTEGER DEFAULT 3,
    deduct_short INTEGER NOT NULL DEFAULT 0,
    perception_tokens INTEGER,
    perception_line TEXT
);

CREATE INDEX timeline_batch_contents ON timeline_batch_members(batch_id);

CREATE TABLE timeline_batches (
    batch_id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    device_id TEXT NOT NULL,
    day TEXT NOT NULL,
    first_at REAL NOT NULL,
    last_at REAL NOT NULL,
    last_known REAL NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    raw_tokens INTEGER NOT NULL DEFAULT 0,
    rule_text TEXT NOT NULL DEFAULT '',
    stats_text TEXT NOT NULL DEFAULT '',
    rule_tokens INTEGER NOT NULL DEFAULT 0,
    sealed INTEGER NOT NULL DEFAULT 0,
    ai_text TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0,
    screenshot_fact_bytes INTEGER
);

CREATE INDEX timeline_ai_pending ON timeline_batches(rule_tokens,retry_at) WHERE sealed=1 AND ai_text IS NULL;

CREATE INDEX timeline_idle ON timeline_batches(last_known) WHERE sealed=0;

CREATE UNIQUE INDEX timeline_open_scope ON timeline_batches(user_id,scope,device_id) WHERE sealed=0;

CREATE TABLE timeline_delivery_heads (
    conversation_id TEXT PRIMARY KEY,
    max_at REAL NOT NULL
);

CREATE TABLE timeline_input_times (
    run_id TEXT NOT NULL REFERENCES dispatch_jobs(job_id),
    input_index INTEGER NOT NULL,
    written_at TEXT NOT NULL,
    PRIMARY KEY(run_id,input_index)
);

CREATE TABLE timeline_rollup_members (
    batch_id INTEGER PRIMARY KEY REFERENCES timeline_batches(batch_id),
    rollup_id INTEGER NOT NULL REFERENCES timeline_rollups(rollup_id)
);

CREATE INDEX timeline_rollup_contents ON timeline_rollup_members(rollup_id);

CREATE TABLE timeline_rollups (
    rollup_id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    device_id TEXT NOT NULL,
    day TEXT NOT NULL,
    sealed INTEGER NOT NULL DEFAULT 0,
    rule_text TEXT NOT NULL DEFAULT '',
    rule_tokens INTEGER NOT NULL DEFAULT 0,
    ai_text TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX timeline_rollup_open ON timeline_rollups(user_id,scope,device_id,day) WHERE sealed=0;

CREATE INDEX timeline_rollup_pending ON timeline_rollups(rule_tokens,retry_at) WHERE sealed=1 AND ai_text IS NULL;

CREATE TABLE tool_batch_calls (
    run_id TEXT NOT NULL REFERENCES tool_batches(run_id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('declared','started','settled')),
    result TEXT,
    PRIMARY KEY(run_id,call_id)
);

CREATE TABLE tool_batches (
    run_id TEXT PRIMARY KEY REFERENCES dispatch_jobs(job_id),
    declaration TEXT NOT NULL
);

CREATE TABLE user_notifications (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    notify_at TEXT,
    UNIQUE(user_id,conversation_id,request_key)
);

CREATE INDEX notification_calendar ON user_notifications(user_id,conversation_id,coalesce(notify_at,created_at),id);

CREATE TABLE user_questions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    reply_device_id TEXT REFERENCES devices,
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    options TEXT NOT NULL,
    multi_select INTEGER NOT NULL DEFAULT 0,
    allow_custom INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    answered_at REAL,
    answered_device_id TEXT REFERENCES devices,
    selected_options TEXT NOT NULL DEFAULT '[]',
    answer_text TEXT NOT NULL DEFAULT '',
    UNIQUE(user_id,conversation_id,run_id,call_id)
);

CREATE INDEX user_questions_history ON user_questions (user_id,conversation_id,seq DESC);

CREATE INDEX user_questions_pending ON user_questions (user_id,conversation_id,seq DESC) WHERE status='pending';
