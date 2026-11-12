-- Real Estate Speed-to-Lead Engine: database schema
-- Paste this whole file into Supabase > SQL Editor > New query > Run.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- agency_settings: always exactly one row (id = 1)
-- ---------------------------------------------------------------------------
create table if not exists public.agency_settings (
    id                  int primary key default 1 check (id = 1),
    agency_name         text not null default 'Skyline Realty Group',
    agent_name          text not null default 'The Skyline Team',
    agent_email         text not null default '',
    agency_phone        text not null default '',
    agency_website      text not null default '',
    brand_color         text not null default '#1F4E79',
    currency            text not null default 'USD',
    target_locations    text not null default 'Downtown, Riverside, Westfield',
    min_budget          bigint not null default 250000,
    hot_lead_threshold  int not null default 8 check (hot_lead_threshold between 1 and 10),
    email_signoff       text not null default E'Warm regards,\nThe Skyline Realty Team',
    custom_instructions text not null default 'Be warm, professional and concise. Never quote prices or promise availability. Invite the lead to reply with a good time for a call.',
    updated_at          timestamptz not null default now()
);

insert into public.agency_settings (id) values (1) on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- leads
-- ---------------------------------------------------------------------------
create table if not exists public.leads (
    id                 uuid primary key default gen_random_uuid(),
    created_at         timestamptz not null default now(),

    -- raw form data
    full_name          text not null,
    email              text not null,
    phone              text not null default '',
    property_interest  text not null default '',
    inquiry_message    text not null default '',
    source             text not null default 'webhook',

    -- AI analysis
    intent             text check (intent in ('Buyer','Seller','Renter','Investor')),
    budget_estimate    text,
    budget_max         bigint,
    preferred_location text,
    lead_score         int check (lead_score between 1 and 10),
    score_reason       text,
    summary            text[],
    email_subject      text,
    email_body         text,
    ai_mode            text,            -- gemini | mock | fallback

    -- workflow
    status             text not null default 'New' check (status in ('New','Contacted','Closed')),
    processing_status  text not null default 'pending',
    email_status       text,            -- sent | mocked | failed
    email_sent_at      timestamptz,
    response_seconds   numeric,
    alert_sent         boolean not null default false,
    alert_channels     text,
    error_log          text
);

create index if not exists leads_created_at_idx on public.leads (created_at desc);
create index if not exists leads_score_idx      on public.leads (lead_score desc);
create index if not exists leads_status_idx     on public.leads (status);

-- ---------------------------------------------------------------------------
-- Security: lock both tables so the public "anon" key can never read them.
-- Your backend and dashboard use the secret service-role key, which bypasses RLS.
-- ---------------------------------------------------------------------------
alter table public.leads           enable row level security;
alter table public.agency_settings enable row level security;
