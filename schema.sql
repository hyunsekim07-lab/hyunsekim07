-- ============================================================
--  카드 연동 자동 적립 — Supabase 스키마 v2
--  Postgres 15+ / Supabase SQL Editor에 그대로 붙여넣기 가능
--
--  v1 대비 달라진 것
--   1. 카드 소유 증명 (미검증 카드는 소급 적립도, 선점도 불가)
--   2. 금액 기반 적립 (누적 n원마다 스탬프 1개) 지원
--   3. 적립을 '목표치 재계산' 방식으로 통일 — 소급/취소가 같은 경로로 처리된다
--   4. 매장 운영자 계정과 매장용 화면 (고객 식별자는 매장별 익명 별칭)
--   5. 쿠폰 사용: 매장 고정 QR을 고객이 스캔
--   6. 알림 파싱 규칙을 서버에서 관리 (앱 업데이트 없이 수정)
--   7. 알림 원문 보존 (파싱 오류 시 재처리)
-- ============================================================

create extension if not exists pgcrypto;   -- gen_random_uuid, gen_random_bytes, hmac
create extension if not exists pg_trgm;    -- 가맹점명 유사도 매칭


-- ============================================================
--  1. 사용자
-- ============================================================
create table profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  nickname    text,
  phone       text,
  created_at  timestamptz not null default now()
);

create or replace function handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into profiles (id) values (new.id);
  return new;
end $$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();

-- 위 트리거는 '앞으로 가입하는' 사용자에게만 돈다.
-- 스키마를 나중에 설치하거나 profiles 를 다시 만들면 기존 사용자에게 행이 없어서,
-- 그 사용자로 카드를 등록하는 순간 외래키 오류가 난다. 설치 시점에 한 번 메워둔다.
insert into profiles (id)
select u.id from auth.users u
left join profiles p on p.id = u.id
where p.id is null;


-- ============================================================
--  2. 매장
-- ============================================================
create table merchants (
  id               uuid primary key default gen_random_uuid(),
  name             text not null,
  category         text,
  lat              double precision,
  lng              double precision,

  -- 제휴 여부. false면 방문 기록만 쌓이고 스탬프는 발급되지 않는다.
  is_partner       boolean not null default false,

  -- 적립 방식
  --   per_visit  : 방문 1회 = 스탬프 1개 (카페)
  --   per_amount : 누적 amount_per_stamp 원마다 스탬프 1개 (슈퍼/편의점)
  --
  -- 슈퍼에 per_visit을 쓰면 담배 한 갑과 8만원 장보기가 같은 대접을 받는다.
  -- 반대로 카페에 per_amount를 쓰면 '10잔에 1잔'이라는 익숙한 약속이 흐려진다.
  -- 순수 % 적립(0.1% 같은)은 일부러 지원하지 않는다. 끝이 보이지 않는 적립은
  -- 고객이 잊어버리고, 그 순간 동네 슈퍼 회원번호와 구별되지 않는다.
  accrual_type     text not null default 'per_visit'
                   check (accrual_type in ('per_visit', 'per_amount')),

  min_amount       integer not null default 0,   -- 적립 대상 최소 결제액(원)
  daily_limit      integer not null default 1,   -- per_visit 전용: 하루 최대 적립 횟수
  amount_per_stamp integer,                      -- per_amount 전용: 5만원 등

  stamps_required  integer not null default 10,  -- 쿠폰 발급 기준
  reward_text      text,                         -- '아메리카노 1잔 무료'
  reward_cost      integer,                      -- 원가(원). 사장님 정산 화면용

  created_at       timestamptz not null default now(),

  constraint accrual_config check (
    (accrual_type = 'per_visit'  and daily_limit >= 1)
    or
    (accrual_type = 'per_amount' and amount_per_stamp is not null and amount_per_stamp > 0)
  )
);

-- 가맹점명 정규화 테이블.
create table merchant_aliases (
  id           uuid primary key default gen_random_uuid(),
  merchant_id  uuid not null references merchants(id) on delete cascade,
  alias        text not null unique,
  source       text,                     -- 'shinhan', 'kb', 'manual' ...
  created_at   timestamptz not null default now()
);

create index on merchant_aliases using gin (alias gin_trgm_ops);


-- 매장 운영자.
-- v1에는 이 개념이 통째로 없었다. 적립은 고객을 부르는 미끼고,
-- 실제로 돈을 내는 쪽은 사장님이므로 매장용 화면이 제품의 절반이다.
create table merchant_staff (
  merchant_id  uuid not null references merchants(id) on delete cascade,
  user_id      uuid not null references profiles(id)  on delete cascade,
  role         text not null default 'staff' check (role in ('owner', 'staff')),
  created_at   timestamptz not null default now(),
  primary key (merchant_id, user_id)
);

-- RLS 정책에서 쓸 헬퍼.
-- security definer가 아니면 merchant_staff 정책이 자기 자신을 참조해 무한 재귀에 빠진다.
create or replace function is_merchant_staff(m uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1 from merchant_staff
    where merchant_id = m and user_id = auth.uid()
  );
$$;


-- 매장 고정 QR. 카운터에 종이 한 장 붙이는 것으로 끝난다.
-- token 자체가 곧 권한이므로 추측 불가능해야 하고, 분실 시 revoke할 수 있어야 한다.
create table merchant_qr (
  id           uuid primary key default gen_random_uuid(),
  merchant_id  uuid not null references merchants(id) on delete cascade,
  token        text not null unique default encode(gen_random_bytes(16), 'hex'),
  label        text,                     -- '카운터', '2번 테이블'
  revoked_at   timestamptz,
  created_at   timestamptz not null default now()
);

create index on merchant_qr (merchant_id) where revoked_at is null;


-- ============================================================
--  3. 카드
-- ============================================================
-- card_hash = hmac(카드번호, 서버 pepper).
--
-- 주의: 카드번호는 엔트로피가 낮다(앞 6자리 BIN 고정 + 마지막 체크섬).
-- pepper가 DB와 같은 곳에 있으면 유출 시 전수 대입으로 카드번호가 복원된다.
-- pepper는 반드시 DB 밖(KMS 등)에 두고, 해시는 서버에서 계산해 넣는다.
create table user_cards (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references profiles(id) on delete cascade,
  card_hash    text,
  issuer       text not null,
  last4        char(4),
  label        text,

  -- ★ 소유 증명. 이게 없으면 카드번호를 아는 사람이 남의 카드를 등록해
  --   과거 거래를 가져가고, 이후 결제까지 실시간으로 넘겨받는다.
  --   금전 목적이 아니라 추적 목적의 공격이라 이상 탐지에도 걸리지 않는다.
  verified_at  timestamptz,

  created_at   timestamptz not null default now(),
  unique (user_id, issuer, last4)
);

create index on user_cards (user_id);

-- 전역 유니크를 '검증된 카드'에만 건다.
-- 전체 unique로 두면 미검증 등록만으로 남의 카드를 선점해버릴 수 있고,
-- 진짜 주인은 "이미 등록된 카드입니다"만 보고 이유를 알 수 없다.
create unique index user_cards_verified_hash_uniq
  on user_cards (card_hash) where verified_at is not null and card_hash is not null;


-- 소유 증명 챌린지.
-- 우리가 이미 그 카드의 거래를 들고 있다는 점을 이용한다.
-- "최근 이 카드로 결제한 금액은?" — 카드번호만 아는 사람은 못 맞힌다.
--
-- 이 테이블은 RLS를 켜고 정책을 하나도 두지 않는다(7번 참고).
-- 정답이 들어 있으므로 사용자가 직접 조회할 수 있으면 검증 자체가 무의미해진다.
create table card_verifications (
  id            uuid primary key default gen_random_uuid(),
  -- 카드당 챌린지는 하나만 만들어 계속 재사용한다.
  -- 호출할 때마다 새로 만들면, 보기를 두세 번 받아 교집합을 내는 것만으로
  -- 정답이 드러난다(정답은 항상 포함되고 오답만 매번 바뀌므로).
  user_card_id  uuid not null unique references user_cards(id) on delete cascade,
  answer_amount integer not null,
  choices       integer[] not null,
  attempts      integer not null default 0,
  passed_at     timestamptz,
  created_at    timestamptz not null default now()
);


-- ============================================================
--  4. 거래
-- ============================================================
create table transactions (
  id           uuid primary key default gen_random_uuid(),

  user_id      uuid references profiles(id) on delete set null,
  card_hash    text,

  merchant_id  uuid references merchants(id) on delete set null,
  raw_merchant text,                     -- 원본 가맹점 문자열 (매칭 실패 시 큐로 사용)

  -- 알림 원문 전체. 파싱 규칙이 틀렸을 때 이게 없으면 그 기간 데이터는 영영 복구 불가다.
  -- 카드사가 문구를 바꾸면 적립이 에러 없이 조용히 멈춘다는 점을 기억할 것.
  raw_payload  text,

  amount       integer not null check (amount > 0),
  paid_at      timestamptz not null,
  source       text not null default 'notification'
               check (source in ('notification', 'pg', 'mydata', 'manual')),

  -- 결제 취소. 행을 지우지 않고 표시만 한다(원장이므로).
  cancelled_at timestamptz,

  -- 비워두면 트리거가 채운다(7-1). 중복이 오면 unique 위반으로 '에러'가 나므로,
  -- 넣는 쪽에서 반드시 on conflict (dedupe_key) do nothing 을 붙여야 한다.
  -- 알림이 두 번 뜨는 것은 예외 상황이 아니라 정상 흐름이다.
  dedupe_key   text unique,

  created_at   timestamptz not null default now()
);

create index on transactions (user_id, paid_at desc);
create index on transactions (card_hash) where card_hash is not null;
create index on transactions (merchant_id) where merchant_id is not null;
create index on transactions (raw_merchant) where merchant_id is null;
create index on transactions (merchant_id, paid_at desc) where cancelled_at is null;


-- ============================================================
--  5. 스탬프 / 쿠폰
-- ============================================================
create table stamp_events (
  id              uuid primary key default gen_random_uuid(),
  transaction_id  uuid references transactions(id) on delete cascade,
  user_id         uuid not null references profiles(id)  on delete cascade,
  merchant_id     uuid not null references merchants(id) on delete cascade,

  -- per_amount 매장에서는 한 거래가 여러 개를 줄 수 있고, 취소 시에는 음수가 된다.
  delta           integer not null default 1,
  reason          text not null default 'earn'
                  check (reason in ('earn', 'revoke', 'adjust')),

  created_at      timestamptz not null default now()
);

create index on stamp_events (user_id, merchant_id);

-- 한 거래당 적립은 한 번뿐. 부분 유니크라 회수(delta < 0)는 같은 거래에 덧붙일 수 있다.
create unique index stamp_events_tx_grant_uniq
  on stamp_events (transaction_id) where delta > 0;

create table coupons (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references profiles(id)  on delete cascade,
  merchant_id   uuid not null references merchants(id) on delete cascade,
  stamps_spent  integer not null,
  reward_text   text,

  -- md5(random()) 앞 8자리는 32비트라 수만 장 수준에서 충돌한다.
  code          text not null unique
                default upper(encode(gen_random_bytes(6), 'hex')),

  issued_at     timestamptz not null default now(),
  expires_at    timestamptz not null default (now() + interval '90 days'),

  -- 사용 처리. 정산 근거이자 분쟁 근거다.
  redeemed_at   timestamptz,
  redeemed_qr   uuid references merchant_qr(id) on delete set null,
  redeemed_lat  double precision,
  redeemed_lng  double precision
);

create index on coupons (user_id) where redeemed_at is null;
create index on coupons (merchant_id, redeemed_at) where redeemed_at is not null;

-- 현재 스탬프 잔액 = 적립 이벤트 합 - 쿠폰으로 사용한 스탬프
--
-- 쿠폰을 받아 간 뒤에 그 결제가 취소되면 balance 가 음수가 될 수 있다.
-- 설계상 맞는 동작이며(다음 적립분에서 자동 상쇄된다) 원장은 그대로 두는 게 맞지만,
-- 화면에 '-1개'로 보이면 이상하므로 앱에서 표시할 때 0으로 눌러야 한다.
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
--  6. 알림 파싱 규칙
-- ============================================================
-- 앱에 정규식을 하드코딩하면, 카드사가 문구를 바꿀 때마다 앱 심사를 기다려야 한다.
-- 그 사이 적립은 조용히 멈추고 사용자는 몇 주 뒤에 눈치챈다.
create table notification_rules (
  id            uuid primary key default gen_random_uuid(),
  issuer        text not null,
  package_name  text,                    -- 'com.shinhancard.smartshinhan'
  pattern       text not null,           -- 정규식
  field_map     jsonb not null,          -- {"amount":1,"merchant":2,"approved_at":3}
  priority      integer not null default 100,
  is_active     boolean not null default true,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index on notification_rules (issuer, priority) where is_active;


-- ============================================================
--  7. 적립 로직
-- ============================================================

-- 7-1. 거래가 들어오면 매장과 사용자를 연결하고 중복 키를 채운다.
create or replace function resolve_transaction()
returns trigger language plpgsql as $$
begin
  -- 가맹점 매칭: 정확 별칭 → 유사도 순
  -- 알림은 길이 제한 때문에 가맹점명이 잘려서 온다('스타벅스경성대...')
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

  -- 카드 → 사용자 매칭. 검증된 카드만 붙인다.
  if new.user_id is null and new.card_hash is not null then
    select user_id into new.user_id
    from user_cards
    where card_hash = new.card_hash and verified_at is not null;
  end if;

  -- 중복 방지 키. unique 제약은 NULL을 걸러주지 않으므로 비워두면 중복이 그냥 통과한다.
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


-- 7-2. 목표치 계산.
-- "지금까지 이 사용자가 이 매장에서 받았어야 할 스탬프 총량"을 거래에서 직접 유도한다.
-- 이월 잔액 같은 상태를 들고 있지 않으므로 소급 적립도, 결제 취소도 재계산 한 번으로 끝난다.
create or replace function earned_target(p_user uuid, p_merchant uuid)
returns integer
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  m      merchants%rowtype;
  total  bigint;
  result integer;
begin
  select * into m from merchants where id = p_merchant;
  if not found or not m.is_partner then
    return 0;   -- 미제휴 매장은 방문 기록만 남는다
  end if;

  if m.accrual_type = 'per_amount' then
    select coalesce(sum(amount), 0) into total
    from transactions
    where user_id = p_user and merchant_id = p_merchant
      and cancelled_at is null
      and amount >= m.min_amount;

    return (total / m.amount_per_stamp)::integer;   -- 정수 나눗셈 = 내림
  end if;

  -- per_visit: 하루 daily_limit 까지만 인정한다.
  -- 주의: created_at(행 생성 시각)이 아니라 paid_at(실제 결제 시각) 기준이어야 한다.
  -- 과거 거래를 소급 처리할 때 created_at을 쓰면 전부 같은 날로 몰려 첫 건 외에 다 걸린다.
  -- 또 Supabase는 UTC로 도므로 KST 기준으로 하루 경계를 잡는다.
  select coalesce(sum(least(d.cnt, m.daily_limit)), 0)::integer into result
  from (
    select date_trunc('day', paid_at at time zone 'Asia/Seoul') as day, count(*) as cnt
    from transactions
    where user_id = p_user and merchant_id = p_merchant
      and cancelled_at is null
      and amount >= m.min_amount
    group by 1
  ) d;

  return result;
end $$;

revoke execute on function earned_target(uuid, uuid) from public;


-- 7-3. 재계산 + 쿠폰 발급.
-- 신규 적립, 소급 적립, 결제 취소가 전부 이 함수 하나를 지난다.
--
-- security definer 인 이유: 스탬프와 쿠폰은 서버만 발급할 수 있어야 한다.
-- stamp_events / coupons 에는 insert 정책을 두지 않고(8번), 이 경로만 RLS를 넘는다.
create or replace function reconcile_stamps(
  p_user uuid, p_merchant uuid, p_tx uuid default null
)
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  m       merchants%rowtype;
  target  integer;
  current integer;
  diff    integer;
  bal     integer;
begin
  if p_user is null or p_merchant is null then
    return;
  end if;

  -- 같은 사용자·매장에 동시에 두 건이 들어오면 잔액을 두 번 읽어 쿠폰이 두 장 나갈 수 있다.
  perform pg_advisory_xact_lock(hashtextextended(p_user::text || p_merchant::text, 0));

  select * into m from merchants where id = p_merchant;
  if not found then
    return;
  end if;

  target := earned_target(p_user, p_merchant);

  select coalesce(sum(delta), 0) into current
  from stamp_events
  where user_id = p_user and merchant_id = p_merchant;

  diff := target - current;

  if diff > 0 then
    insert into stamp_events (transaction_id, user_id, merchant_id, delta, reason)
    values (p_tx, p_user, p_merchant, diff, 'earn')
    on conflict (transaction_id) where delta > 0 do nothing;
  elsif diff < 0 then
    -- 취소로 목표치가 줄었다. 원장이므로 지우지 않고 음수로 상쇄한다.
    insert into stamp_events (transaction_id, user_id, merchant_id, delta, reason)
    values (null, p_user, p_merchant, diff, 'revoke');
  else
    return;   -- 변화 없음 (재시도, 이미 처리된 소급 건)
  end if;

  -- 쿠폰 발급. 소급 처리 직후에는 기준의 몇 배가 한 번에 쌓일 수 있으므로 반복한다.
  loop
    select balance into bal
    from v_stamp_balance
    where user_id = p_user and merchant_id = p_merchant;

    exit when coalesce(bal, 0) < m.stamps_required;

    insert into coupons (user_id, merchant_id, stamps_spent, reward_text)
    values (p_user, p_merchant, m.stamps_required, m.reward_text);
  end loop;
end $$;

revoke execute on function reconcile_stamps(uuid, uuid, uuid) from public;


create or replace function on_transaction_written()
returns trigger language plpgsql as $$
begin
  perform reconcile_stamps(new.user_id, new.merchant_id, new.id);
  return null;
end $$;

-- 참고: Postgres의 after-row 트리거는 행마다 즉시 돌지 않고 문장이 끝난 뒤 몰아서 돈다.
-- 그래서 3건을 한 번에 insert 하면 첫 트리거가 이미 3건을 다 보고 차액을 한 줄로 쓴다
-- (delta 1 이 세 줄이 아니라 delta 3 이 한 줄). 목표치 방식이라 합계는 어느 쪽이든 같다.
create trigger trg_transaction_stamp
  after insert on transactions
  for each row execute function on_transaction_written();

-- 취소되거나 뒤늦게 매칭된 거래도 같은 경로를 탄다.
create trigger trg_transaction_stamp_upd
  after update of cancelled_at, merchant_id, user_id on transactions
  for each row execute function on_transaction_written();


-- ============================================================
--  8. 카드 소유 증명
-- ============================================================
-- 8-1. 챌린지 생성. 그 카드의 실제 최근 결제 금액 + 그럴듯한 오답 8개.
--
-- 보기 수(9)가 허용 시도 수(3, 8-2)보다 반드시 많아야 한다. 오답이 2개뿐이던 이전
-- 버전은 보기 3개·시도 3번이라 답을 몰라도 다 찍으면 무조건 통과하는 구조였다.
create or replace function start_card_verification(p_card uuid)
returns integer[]
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  c        user_cards%rowtype;
  ch       card_verifications%rowtype;
  real_amt integer;
  decoys   integer[];
  picks    integer[];
begin
  select * into c from user_cards where id = p_card;
  if not found or c.user_id <> auth.uid() then
    raise exception 'card not found';
  end if;
  if c.verified_at is not null then
    raise exception 'already verified';
  end if;
  if c.card_hash is null then
    raise exception 'card hash required';
  end if;

  -- 이미 만들어둔 챌린지가 있으면 그대로 돌려준다. 보기가 바뀌면 안 된다.
  select * into ch from card_verifications where user_card_id = p_card;
  if found then
    if ch.attempts >= 3 then
      raise exception 'verification locked';
    end if;
    return ch.choices;
  end if;

  select amount into real_amt
  from transactions
  where card_hash = c.card_hash
  order by paid_at desc
  limit 1;

  -- 아직 이 카드의 거래가 하나도 없으면 이 방식으로는 증명할 수 없다.
  -- (알림 파싱만 돌리는 단계에서는 정상적인 상황이다. 다른 수단으로 보내야 한다)
  if real_amt is null then
    raise exception 'not enough data to verify';
  end if;

  -- 오답은 실제 금액과 자릿수가 비슷해야 한다. 티 나면 찍어서 맞힌다.
  -- 8개(위 4개, 아래 4개)를 만들어 보기를 총 9개로 늘린다.
  select array_agg(greatest(1000, real_amt + (n * 1000) + (random() * 4000)::integer))
         || array_agg(greatest(1000, real_amt - (n * 1000) - (random() * 4000)::integer))
    into decoys
  from generate_series(1, 4) as n;

  select array_agg(v order by random()) into picks
  from unnest(real_amt || decoys) as v;

  insert into card_verifications (user_card_id, answer_amount, choices)
  values (p_card, real_amt, picks);

  return picks;
end $$;


-- 8-2. 정답 제출. 통과하면 검증 도장을 찍고, 그 순간 소급 적립이 돈다.
create or replace function submit_card_verification(p_card uuid, p_amount integer)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v user_cards%rowtype;
  ch card_verifications%rowtype;
begin
  select * into v from user_cards where id = p_card;
  if not found or v.user_id <> auth.uid() then
    raise exception 'card not found';
  end if;

  select * into ch
  from card_verifications
  where user_card_id = p_card and passed_at is null;

  if not found then
    raise exception 'no active challenge';
  end if;

  -- 시도 횟수(3)가 보기 수(9)보다 반드시 적어야 한다. 같거나 많으면 답을 몰라도
  -- 남은 보기를 소거법으로 다 찍어서 무조건 통과하게 된다.
  if ch.attempts >= 3 then
    raise exception 'too many attempts';
  end if;

  update card_verifications set attempts = attempts + 1 where id = ch.id;

  if ch.answer_amount <> p_amount then
    return false;
  end if;

  update card_verifications set passed_at = now() where id = ch.id;
  update user_cards set verified_at = now() where id = p_card;
  return true;
end $$;


-- 8-3. 검증된 순간 소급 적립.
-- 등록 시점이 아니라 '검증 시점'에 도는 것이 핵심이다.
--
-- security definer 가 필수다: 주인 없는 거래(user_id is null)는 RLS상 아무에게도
-- 보이지 않으므로, invoker 권한으로 돌면 아래 update가 0건을 건드리고 조용히 끝난다.
create or replace function backfill_on_card_verified()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  r record;
begin
  if new.verified_at is null or old.verified_at is not null then
    return new;
  end if;
  if new.card_hash is null then
    return new;
  end if;

  update transactions
  set user_id = new.user_id
  where card_hash = new.card_hash and user_id is null;

  -- 매장별로 한 번씩만 재계산하면 된다. 목표치 방식이라 건별로 돌 필요가 없다.
  for r in
    select distinct merchant_id
    from transactions
    where card_hash = new.card_hash
      and user_id = new.user_id
      and merchant_id is not null
  loop
    perform reconcile_stamps(new.user_id, r.merchant_id, null);
  end loop;

  return new;
end $$;

create trigger trg_backfill_card
  after update of verified_at on user_cards
  for each row execute function backfill_on_card_verified();


-- ============================================================
--  9. 쿠폰 사용
-- ============================================================
-- 고객이 매장 QR을 스캔하면 사용 처리된다. 사장님은 종이 한 장 붙인 것 외에 할 일이 없다.
--
-- 코드 무차별 대입은 구조적으로 막혀 있다: 아래 update는 user_id = auth.uid() 를
-- 조건에 포함하므로, 남의 코드를 알아내도 자기 계정으로는 쓸 수 없다.
-- 재사용(스크린샷 돌려쓰기)은 redeemed_at is null 조건이 원자적으로 막는다.
create or replace function redeem_coupon(
  p_code     text,
  p_qr_token text,
  p_lat      double precision default null,
  p_lng      double precision default null
)
-- 반환 컬럼 이름에 접두사를 붙인 이유: plpgsql은 OUT 파라미터와 이름이 같은 컬럼을
-- 변수로 해석한다. 'redeemed_at' 을 그대로 쓰면 아래 update의 재사용 방지 조건이
-- 'NULL is null' 로 바뀌거나 ambiguous 오류가 난다.
returns table (out_coupon_id uuid, out_merchant_name text,
               out_reward_text text, out_redeemed_at timestamptz)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  q merchant_qr%rowtype;
  c coupons%rowtype;
begin
  if auth.uid() is null then
    raise exception 'authentication required';
  end if;

  select * into q from merchant_qr where token = p_qr_token and revoked_at is null;
  if not found then
    raise exception 'invalid qr';
  end if;

  update coupons
  set redeemed_at  = now(),
      redeemed_qr  = q.id,
      redeemed_lat = p_lat,
      redeemed_lng = p_lng
  where code = p_code
    and user_id = auth.uid()
    and merchant_id = q.merchant_id
    and redeemed_at is null
    and expires_at > now()
  returning * into c;

  if not found then
    -- 왜 실패했는지 구분해 알려준다. 여기서는 이미 소유자임이 확인된 쿠폰만 다룬다.
    perform 1 from coupons
    where code = p_code and user_id = auth.uid() and redeemed_at is not null;
    if found then
      raise exception 'already redeemed';
    end if;
    raise exception 'coupon not usable here';
  end if;

  return query
  select c.id, m.name, c.reward_text, c.redeemed_at
  from merchants m where m.id = c.merchant_id;
end $$;


-- ============================================================
--  10. 매장용 화면
-- ============================================================
-- 이 뷰들은 일부러 security_invoker 를 켜지 않는다.
-- 사장님에게 transactions 를 RLS로 직접 열어주면 PostgREST로 user_id 까지 긁을 수 있다.
-- 대신 뷰가 스스로 is_merchant_staff() 로 권한을 확인하고, 필요한 컬럼만 내보낸다.
--
-- 고객 식별자는 매장마다 다른 값이 나오도록 만든다.
-- 사장님은 "이 단골이 12번 왔다"는 알 수 있지만 그게 누구인지는 알 수 없고,
-- 다른 매장의 기록과 맞춰볼 수도 없다.
create view v_merchant_customers as
select
  t.merchant_id,
  encode(hmac(t.user_id::text, t.merchant_id::text, 'sha256'), 'hex') as customer_ref,
  count(*)                                   as visits,
  sum(t.amount)                              as total_amount,
  max(t.paid_at)                             as last_visit
from transactions t
where t.user_id is not null
  and t.cancelled_at is null
  and is_merchant_staff(t.merchant_id)
group by t.merchant_id, t.user_id;

-- 사장님이 가장 먼저 보고 싶어하는 화면: 공짜로 나간 게 몇 잔이고 원가가 얼마인가.
create view v_merchant_redemptions as
select
  c.merchant_id,
  date_trunc('day', c.redeemed_at at time zone 'Asia/Seoul') as day,
  count(*)                                      as redeemed_count,
  count(*) * coalesce(m.reward_cost, 0)         as estimated_cost
from coupons c
join merchants m on m.id = c.merchant_id
where c.redeemed_at is not null
  and is_merchant_staff(c.merchant_id)
group by c.merchant_id, 2, m.reward_cost;

-- 정규화 작업 큐 (운영자용)
create view v_unmatched_merchants with (security_invoker = on) as
select raw_merchant, count(*) as cnt, max(paid_at) as last_seen
from transactions
where merchant_id is null and raw_merchant is not null
group by raw_merchant
order by cnt desc;


-- ============================================================
--  11. RLS
-- ============================================================
-- Supabase는 public 스키마를 PostgREST로 노출하고, anon key는 앱에 박혀 있어
-- 사실상 공개값이다. 즉 anon key로 가능한 모든 일은 공격자도 할 수 있다.
-- 테이블을 추가할 때 RLS 활성화를 같은 마이그레이션에 반드시 함께 넣을 것.
alter table profiles           enable row level security;
alter table user_cards         enable row level security;
alter table card_verifications enable row level security;
alter table transactions       enable row level security;
alter table stamp_events       enable row level security;
alter table coupons            enable row level security;
alter table merchants          enable row level security;
alter table merchant_aliases   enable row level security;
alter table merchant_staff     enable row level security;
alter table merchant_qr        enable row level security;
alter table notification_rules enable row level security;

-- 본인 데이터만
create policy own_profile on profiles
  for all using (id = auth.uid());

-- update 정책을 일부러 두지 않는다.
-- for all 로 열면 사용자가 verified_at 을 스스로 now() 로 찍어
-- 소유 증명을 통째로 우회할 수 있다. verified_at 은 8번 함수만 건드린다.
create policy cards_select on user_cards
  for select using (user_id = auth.uid());

-- verified_at is null 조건이 핵심이다. 이게 없으면 사용자가 insert 할 때
-- verified_at 을 직접 now() 로 채워 넣어 챌린지 자체를 건너뛸 수 있다
-- (update 정책이 없다고 안심한 것과 같은 구멍이 insert 에는 그대로 열려 있었다).
-- 검증은 반드시 8번의 security definer 함수(submit_card_verification)만 거치게 한다.
create policy cards_insert on user_cards
  for insert with check (user_id = auth.uid() and verified_at is null);

create policy cards_delete on user_cards
  for delete using (user_id = auth.uid());

-- card_verifications 는 정책을 두지 않는다.
-- 정답(answer_amount)이 들어 있으므로 사용자가 조회할 수 있으면 검증이 무의미해진다.
-- 8번의 security definer 함수로만 접근한다.

-- 알림 파싱 경로에서는 사용자 기기가 직접 거래를 넣으므로 insert를 열어둔다.
-- 이 구조는 클라이언트를 믿는다는 뜻이다. daily_limit → 이상 패턴 탐지 →
-- 사장님 육안 확인의 3중으로 누르고, 근본 해결은 서버 대 서버 경로(마이데이터/VAN)다.
create policy tx_select on transactions
  for select using (user_id = auth.uid());

create policy tx_insert on transactions
  for insert with check (user_id = auth.uid());

-- update/delete 정책 없음. 금액을 사후에 늘리거나 취소를 되돌릴 이유가 없고,
-- 취소 처리는 데이터 공급 경로(서비스 키)에서 들어온다.

-- 스탬프와 쿠폰은 읽기만. 발급은 reconcile_stamps, 사용은 redeem_coupon 만 한다.
create policy own_stamps on stamp_events
  for select using (user_id = auth.uid());

create policy own_coupons on coupons
  for select using (user_id = auth.uid());

-- 매장 정보와 별칭, 파싱 규칙은 조회만. 수정은 서비스 키로만.
create policy public_merchants on merchants
  for select using (true);

create policy public_aliases on merchant_aliases
  for select using (true);

create policy public_rules on notification_rules
  for select using (is_active);

-- 운영자는 자기 매장 것만
create policy own_staff_rows on merchant_staff
  for select using (user_id = auth.uid());

create policy staff_qr on merchant_qr
  for select using (is_merchant_staff(merchant_id));


-- ============================================================
--  12. 시드 데이터 (테스트용)
-- ============================================================
with m as (
  insert into merchants (name, category, is_partner, accrual_type,
                         min_amount, daily_limit, stamps_required,
                         reward_text, reward_cost)
  values ('카페 예시점', 'cafe', true, 'per_visit',
          3000, 1, 10, '아메리카노 1잔 무료', 1200)
  returning id
)
insert into merchant_aliases (merchant_id, alias, source)
select m.id, a.alias, a.source
from m,
     (values ('카페예시점', 'shinhan'),
             ('(주)카페예시', 'kb')) as a(alias, source);

-- 금액 기반 매장 예시: 5만원마다 스탬프 1개, 10개면 5천원 할인 (실질 1%)
with m as (
  insert into merchants (name, category, is_partner, accrual_type,
                         min_amount, amount_per_stamp, stamps_required,
                         reward_text, reward_cost)
  values ('동네슈퍼 예시점', 'grocery', true, 'per_amount',
          0, 50000, 10, '5,000원 할인', 5000)
  returning id
)
insert into merchant_aliases (merchant_id, alias, source)
select m.id, '동네슈퍼예시', 'shinhan' from m;


-- ============================================================
--  13. 자주 쓸 쿼리
-- ============================================================
-- 내 스탬프 현황
-- select * from v_stamp_balance where user_id = auth.uid();

-- 매칭 실패한 가맹점 문자열
-- select * from v_unmatched_merchants limit 50;

-- 카드 등록 → 검증 → 소급 적립
-- insert into user_cards (user_id, card_hash, issuer, last4) values (...) returning id;
-- select start_card_verification('<card_id>');
-- select submit_card_verification('<card_id>', 4500);

-- 쿠폰 사용
-- select * from redeem_coupon('A1B2C3D4E5F6', '<qr_token>', 35.14, 129.09);


-- ============================================================
--  14. 권한
-- ============================================================
-- 뷰는 RLS를 우회하므로(10번 참고) 스스로 권한을 확인한다. 조회 권한만 준다.
grant select on v_stamp_balance, v_merchant_customers, v_merchant_redemptions to authenticated;

-- 클라이언트가 호출해야 하는 함수만 열어준다.
-- reconcile_stamps / earned_target 은 위에서 revoke 했다. 직접 호출 금지.
grant execute on function is_merchant_staff(uuid)                      to authenticated;
grant execute on function start_card_verification(uuid)                to authenticated;
grant execute on function submit_card_verification(uuid, integer)      to authenticated;
grant execute on function redeem_coupon(text, text, double precision, double precision)
                                                                       to authenticated;


-- ============================================================
--  15. 설치 후 점검 (실제로 돌려서 확인한 것들)
-- ============================================================
-- 테이블 수와 RLS 적용 수가 다르면 그 차이가 곧 공개 테이블이다.
-- select
--   (select count(*) from pg_tables where schemaname='public')                 as 테이블,
--   (select count(*) from pg_tables where schemaname='public' and rowsecurity) as RLS켜짐,
--   (select count(*) from pg_policies where schemaname='public')               as 정책;
--   -- 11 / 11 / 13

-- 원장 불변식: 거래에서 유도한 목표치와 원장 합계가 항상 같아야 한다.
-- 몇 번을 재실행하든, 중간에 취소를 섞든 이 등식은 깨지지 않는다.
-- select
--   earned_target(u.id, m.id)                                                   as 목표치,
--   (select coalesce(sum(delta),0) from stamp_events
--      where user_id = u.id and merchant_id = m.id)                             as 원장합계
-- from profiles u, merchants m;

-- 소유 증명이 실제로 막는지 (관리자 권한으로는 확인되지 않는다)
-- set local role authenticated;
-- set local request.jwt.claims = '{"sub":"<user-uuid>","role":"authenticated"}';
--
--   select count(*) from card_verifications;   -- 0  정답이 들어 있으므로 아무도 못 본다
--   update user_cards set verified_at = now() where card_hash = '...';  -- UPDATE 0
--   insert into stamp_events (user_id, merchant_id, delta) values (...);
--     -- new row violates row-level security policy 로 실패해야 정상
