-- ============================================================
--  카드 연동 자동 적립 (card-linked loyalty) — Supabase 스키마
--  Postgres 15+ / Supabase SQL Editor에 그대로 붙여넣기 가능
-- ============================================================

create extension if not exists pgcrypto;   -- gen_random_uuid, hmac
create extension if not exists pg_trgm;    -- 가맹점명 유사도 매칭


-- ============================================================
--  1. 사용자
-- ============================================================
-- Supabase는 auth.users를 자체 관리하므로, 부가 정보만 별도 테이블로 둔다.
create table profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  nickname    text,
  phone       text,
  created_at  timestamptz not null default now()
);

-- 회원가입 시 profiles 행 자동 생성
create or replace function handle_new_user()
returns trigger language plpgsql security definer as $$
begin
  insert into profiles (id) values (new.id);
  return new;
end $$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();


-- ============================================================
--  2. 카드
-- ============================================================
-- card_hash 는 카드 식별값의 단방향 해시. 원본 카드번호는 절대 저장하지 않는다.
--
--   [갈래 A: PG/VAN 연동]  card_hash = hmac(카드번호, 서버 pepper)
--   [갈래 B: 알림 파싱]    카드사 알림에는 카드번호가 없다. 기껏해야 뒤 4자리와
--                          카드 별칭뿐. 대신 알림은 "사용자 본인 폰"에서 오므로
--                          user_id를 이미 알고 있다 → card_hash가 없어도 된다.
--
-- 즉 card_hash는 갈래 A에서만 진가를 발휘한다. (미가입자 소급 적립)
create table user_cards (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references profiles(id) on delete cascade,
  card_hash   text unique,              -- 전역 유니크: 한 카드는 한 사람에게만 귀속
  issuer      text not null,            -- '신한', '국민', '현대' ...
  last4       char(4),
  label       text,                     -- 사용자가 붙인 이름 ('메인 체크')
  created_at  timestamptz not null default now(),
  unique (user_id, issuer, last4)
);

create index on user_cards (user_id);


-- ============================================================
--  3. 매장
-- ============================================================
create table merchants (
  id               uuid primary key default gen_random_uuid(),
  name             text not null,        -- 표시용 이름 ('스타벅스 경성대점')
  category         text,                 -- 'cafe', 'restaurant' ...
  lat              double precision,
  lng              double precision,

  -- 제휴 여부. false면 방문 기록만 쌓이고 스탬프는 발급되지 않는다.
  -- (미제휴 매장 데이터가 곧 영업 자료가 된다)
  is_partner       boolean not null default false,

  -- 적립 규칙
  min_amount       integer not null default 0,   -- 최소 결제액(원). 0이면 제한 없음
  daily_limit      integer not null default 1,   -- 하루 최대 적립 횟수
  stamps_required  integer not null default 10,  -- 쿠폰 발급 기준
  reward_text      text,                         -- '아메리카노 1잔 무료'

  created_at       timestamptz not null default now()
);

-- 가맹점명 정규화 테이블.
-- 카드사마다 표기가 제각각이라 (스타벅스강남2, (주)스타벅스코리아, SPC...)
-- 별칭을 손으로 쌓아가는 게 현실적이다.
create table merchant_aliases (
  id           uuid primary key default gen_random_uuid(),
  merchant_id  uuid not null references merchants(id) on delete cascade,
  alias        text not null,
  source       text,                     -- 'shinhan', 'kb', 'manual' ...
  created_at   timestamptz not null default now(),
  unique (alias)
);

create index on merchant_aliases using gin (alias gin_trgm_ops);


-- ============================================================
--  4. 거래
-- ============================================================
create table transactions (
  id           uuid primary key default gen_random_uuid(),

  -- 둘 중 최소 하나는 채워진다.
  user_id      uuid references profiles(id) on delete set null,
  card_hash    text,

  merchant_id  uuid references merchants(id) on delete set null,
  raw_merchant text,                     -- 원본 가맹점 문자열 (매칭 실패 시 큐로 사용)

  amount       integer not null check (amount > 0),
  paid_at      timestamptz not null,
  source       text not null default 'notification'
               check (source in ('notification', 'pg', 'manual')),

  -- 중복 방지: 알림이 두 번 뜨거나 재설치 시 같은 건이 재전송될 수 있다.
  dedupe_key   text unique,             -- 비워두면 트리거가 채운다 (6-1 참고)

  created_at   timestamptz not null default now()
);

create index on transactions (user_id, paid_at desc);
create index on transactions (card_hash) where card_hash is not null;
create index on transactions (merchant_id) where merchant_id is not null;
-- 아직 매칭 안 된 거래 = 정규화 작업 큐
create index on transactions (raw_merchant) where merchant_id is null;


-- ============================================================
--  5. 스탬프 / 쿠폰
-- ============================================================
-- 스탬프를 카운터 컬럼 하나로 두지 않고 이벤트로 쌓는다.
-- 이유: 결제 취소 시 회수 가능, 하루 제한 계산 가능, 분쟁 시 추적 가능.
create table stamp_events (
  id              uuid primary key default gen_random_uuid(),
  transaction_id  uuid references transactions(id) on delete cascade,
  user_id         uuid not null references profiles(id) on delete cascade,
  merchant_id     uuid not null references merchants(id) on delete cascade,
  delta           integer not null default 1,   -- 회수 시 음수
  created_at      timestamptz not null default now()
);

create index on stamp_events (user_id, merchant_id);

-- 한 거래당 적립은 한 번뿐. 단 부분 유니크라 회수(delta < 0) 이벤트는 같은 거래에
-- 덧붙일 수 있다. 컬럼에 그냥 unique를 걸면 위 주석의 '회수 시 음수'가 아예 불가능해진다.
create unique index stamp_events_tx_grant_uniq
  on stamp_events (transaction_id) where delta > 0;

create table coupons (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references profiles(id) on delete cascade,
  merchant_id   uuid not null references merchants(id) on delete cascade,
  stamps_spent  integer not null,
  reward_text   text,
  -- md5(random()) 앞 8자리는 32비트라 수만 장 수준에서 생일 문제로 충돌한다.
  code          text not null unique
                default upper(encode(gen_random_bytes(6), 'hex')),
  issued_at     timestamptz not null default now(),
  expires_at    timestamptz not null default (now() + interval '90 days'),
  redeemed_at   timestamptz
);

create index on coupons (user_id) where redeemed_at is null;

-- 현재 스탬프 잔액 = 적립 이벤트 합 - 쿠폰으로 사용한 스탬프
-- security_invoker: 이게 없으면 뷰가 생성자 권한으로 돌아서 RLS를 통과해버린다.
create view v_stamp_balance with (security_invoker = on) as
select
  s.user_id,
  s.merchant_id,
  m.name          as merchant_name,
  m.stamps_required,
  m.reward_text,
  coalesce(sum(s.delta), 0) - coalesce(c.spent, 0) as balance
from stamp_events s
join merchants m on m.id = s.merchant_id
left join lateral (
  select sum(stamps_spent) as spent
  from coupons
  where coupons.user_id = s.user_id
    and coupons.merchant_id = s.merchant_id
) c on true
group by s.user_id, s.merchant_id, m.name, m.stamps_required, m.reward_text, c.spent;


-- ============================================================
--  6. 자동화 트리거
-- ============================================================

-- 6-1. 거래가 들어오면 매장과 사용자를 연결한다.
create or replace function resolve_transaction()
returns trigger language plpgsql as $$
begin
  -- 가맹점 매칭: 정확 별칭 → 유사도 순
  if new.merchant_id is null and new.raw_merchant is not null then
    select merchant_id into new.merchant_id
    from merchant_aliases
    where alias = new.raw_merchant;

    if new.merchant_id is null then
      select merchant_id into new.merchant_id
      from merchant_aliases
      where similarity(alias, new.raw_merchant) > 0.6
      order by similarity(alias, new.raw_merchant) desc
      limit 1;
    end if;
  end if;

  -- 카드 → 사용자 매칭 (갈래 A 경로)
  if new.user_id is null and new.card_hash is not null then
    select user_id into new.user_id
    from user_cards
    where card_hash = new.card_hash;
  end if;

  -- 중복 방지 키. 클라이언트가 안 넣어주면 여기서 만든다.
  -- unique 제약은 NULL을 걸러주지 않으므로, 비워둔 채로 두면 중복이 그냥 통과한다.
  -- paid_at::text 대신 epoch를 쓰는 이유: 텍스트 표기는 세션 TimeZone에 따라 달라진다.
  if new.dedupe_key is null then
    new.dedupe_key := md5(
      coalesce(new.card_hash, new.user_id::text, '') || '|' ||
      coalesce(new.raw_merchant, new.merchant_id::text, '') || '|' ||
      new.amount::text || '|' ||
      extract(epoch from new.paid_at)::bigint::text
    );
  end if;

  return new;
end $$;

create trigger trg_resolve_transaction
  before insert on transactions
  for each row execute function resolve_transaction();


-- 6-2. 적립 판정 본체.
-- 실시간 트리거와 소급 처리가 반드시 같은 규칙을 타야 하므로 함수로 뺀다.
-- (예전에는 소급 경로가 daily_limit 검사도, 쿠폰 발급도 통째로 건너뛰었다.
--  그래서 "스탬프는 12개인데 쿠폰은 없음" 같은 상태가 생겼다)
--
-- security definer 인 이유: 스탬프와 쿠폰은 서버만 발급할 수 있어야 한다.
-- stamp_events / coupons 에는 insert 정책을 아예 두지 않고(7번), 이 함수만 RLS를 넘는다.
create or replace function try_grant_stamp(t transactions)
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  m           merchants%rowtype;
  day_start   timestamptz;
  today_count integer;
  bal         integer;
begin
  if t.user_id is null or t.merchant_id is null then
    return;
  end if;

  select * into m from merchants where id = t.merchant_id;

  if not found or not m.is_partner or t.amount < m.min_amount then
    return;   -- 방문 기록만 남고 스탬프는 없음
  end if;

  -- 하루 적립 제한.
  -- 주의: stamp_events.created_at(행 생성 시각)이 아니라 transactions.paid_at
  -- (실제 결제 시각) 기준으로 세야 한다. 과거 거래를 소급 처리할 때 created_at을
  -- 쓰면 전부 같은 날로 몰려서 첫 건 외에 모두 제한에 걸린다.
  -- 또 Supabase는 UTC로 도므로 KST 기준으로 하루 경계를 잡는다.
  day_start := date_trunc('day', t.paid_at at time zone 'Asia/Seoul')
               at time zone 'Asia/Seoul';

  select count(*) into today_count
  from stamp_events s
  join transactions tx on tx.id = s.transaction_id
  where s.user_id = t.user_id
    and s.merchant_id = t.merchant_id
    and tx.paid_at >= day_start
    and tx.paid_at <  day_start + interval '1 day'
    and s.delta > 0;

  if today_count >= m.daily_limit then
    return;
  end if;

  -- on conflict: 소급 처리를 몇 번 돌려도 같은 거래가 두 번 찍히지 않는다.
  insert into stamp_events (transaction_id, user_id, merchant_id)
  values (t.id, t.user_id, t.merchant_id)
  on conflict (transaction_id) where delta > 0 do nothing;

  if not found then
    return;   -- 이미 적립된 거래
  end if;

  -- 쿠폰 발급 판정
  select balance into bal
  from v_stamp_balance
  where user_id = t.user_id and merchant_id = t.merchant_id;

  if coalesce(bal, 0) >= m.stamps_required then
    insert into coupons (user_id, merchant_id, stamps_spent, reward_text)
    values (t.user_id, t.merchant_id, m.stamps_required, m.reward_text);
  end if;
end $$;

-- security definer 함수를 사용자가 직접 호출해 스탬프를 찍는 것을 막는다.
revoke execute on function try_grant_stamp(transactions) from public;


create or replace function grant_stamp()
returns trigger language plpgsql as $$
begin
  perform try_grant_stamp(new);
  return new;
end $$;

create trigger trg_grant_stamp
  after insert on transactions
  for each row execute function grant_stamp();


-- 6-3. ★ 소급 적립
-- 카드를 새로 등록하면, 그 카드로 쌓여 있던 과거 거래가 통째로 붙는다.
-- "가입했더니 이미 스탬프 7개" 를 만들어내는 부분.
--
-- security definer 가 필수다: 주인 없는 거래(user_id is null)는 RLS상 아무에게도
-- 보이지 않는다. invoker 권한으로 돌면 아래 update가 0건을 건드리고 조용히 끝난다.
create or replace function backfill_on_card_register()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  t transactions%rowtype;
begin
  if new.card_hash is null then
    return new;
  end if;

  update transactions
  set user_id = new.user_id
  where card_hash = new.card_hash and user_id is null;

  -- 붙은 거래들에 대해 스탬프 재계산.
  -- 실시간 적립과 똑같은 규칙(제휴 여부 / 최소금액 / 하루 제한 / 쿠폰 발급)을 탄다.
  -- 중복은 try_grant_stamp 안의 on conflict 가 막으므로 여기서 거를 필요가 없다.
  for t in
    select *
    from transactions
    where card_hash = new.card_hash
      and user_id = new.user_id
      and merchant_id is not null
    order by paid_at
  loop
    perform try_grant_stamp(t);
  end loop;

  return new;
end $$;

create trigger trg_backfill_card
  after insert on user_cards
  for each row execute function backfill_on_card_register();


-- ============================================================
--  7. RLS — Supabase에서는 필수
-- ============================================================
alter table profiles         enable row level security;
alter table user_cards       enable row level security;
alter table transactions     enable row level security;
alter table stamp_events     enable row level security;
alter table coupons          enable row level security;
alter table merchants        enable row level security;
-- merchant_aliases 를 빼먹으면 안 된다. Supabase는 public 스키마를 PostgREST로
-- 노출하므로, RLS가 꺼진 테이블은 anon 키로 읽기는 물론 쓰기까지 열린다.
-- 별칭 테이블이 곧 매칭 로직의 신뢰 기반이라, 조작되면 적립이 엉뚱한 매장으로 간다.
alter table merchant_aliases enable row level security;

-- 본인 데이터만
create policy own_profile on profiles
  for all using (id = auth.uid());

create policy own_cards on user_cards
  for all using (user_id = auth.uid());

-- 알림 파싱 경로에서는 사용자 기기가 직접 거래를 넣으므로 insert를 열어둔다.
-- with check 를 명시해 남의 id로는 못 넣게 한다.
create policy own_tx on transactions
  for all using (user_id = auth.uid())
  with check (user_id = auth.uid());

-- 스탬프와 쿠폰은 읽기만 허용한다.
-- for all 로 두면 사용자가 stamp_events / coupons 에 직접 insert 해서
-- 스탬프와 쿠폰을 무한히 찍어낼 수 있다. 발급은 try_grant_stamp 만 한다(6-2).
-- 쿠폰 사용 처리(redeemed_at)도 같은 이유로 서비스 키나 별도 RPC로 돌려야 한다.
create policy own_stamps on stamp_events
  for select using (user_id = auth.uid());

create policy own_coupons on coupons
  for select using (user_id = auth.uid());

-- 매장 정보는 누구나 조회 가능, 수정은 서비스 키로만
create policy public_merchants on merchants
  for select using (true);

create policy public_aliases on merchant_aliases
  for select using (true);


-- ============================================================
--  8. 시드 데이터 (테스트용)
-- ============================================================
-- 매장과 별칭을 CTE로 한 번에 넣는다. (id를 손으로 복사할 필요 없음)
-- 별칭에는 실제 카드사 알림에서 관측된 문자열을 그대로 넣으면 된다.
with m as (
  insert into merchants (name, category, is_partner, min_amount, daily_limit,
                         stamps_required, reward_text)
  values ('카페 예시점', 'cafe', true, 3000, 1, 10, '아메리카노 1잔 무료')
  returning id
)
insert into merchant_aliases (merchant_id, alias, source)
select m.id, a.alias, a.source
from m,
     (values ('카페예시점', 'shinhan'),
             ('(주)카페예시', 'kb')) as a(alias, source);


-- ============================================================
--  9. 자주 쓸 쿼리
-- ============================================================

-- 내 스탬프 현황
-- select * from v_stamp_balance where user_id = auth.uid();

-- 매칭 실패한 가맹점 문자열 (정규화 작업 큐)
-- select raw_merchant, count(*) as cnt
-- from transactions
-- where merchant_id is null
-- group by raw_merchant
-- order by cnt desc
-- limit 50;

-- 특정 매장의 단골 수 (= 영업용 자료)
-- select count(distinct user_id)
-- from transactions
-- where merchant_id = '<merchant_id>'
--   and paid_at > now() - interval '90 days';
