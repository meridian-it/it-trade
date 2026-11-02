// Shapes of the admin API (backend/app/api/admin/*).

export type Page<T> = { items: T[]; total: number; page: number; per_page: number };

export type AdminUser = { id: number; email: string; name: string | null; role: string };

export type OrderStatus = "new" | "confirmed" | "ordered" | "ready" | "shipped" | "completed" | "cancelled";
export type OrderSource = "site" | "phone" | "admin";
export type DeliveryMethod = "pickup" | "courier" | "transport";

// Every product has 4 prices: who buys (retail customers / B2B companies) x how they pay.
export type Audience = "retail" | "wholesale";
export type PaymentMethod = "cash" | "cashless";
export type PriceType = `${Audience}_${PaymentMethod}`;

export type OrderListItem = {
  id: number;
  number: string;
  status: OrderStatus;
  source: OrderSource;
  customer_name: string;
  phone: string;
  total: number;
  items_count: number;
  created_at: string;
  payment_method: PaymentMethod;
  price_type: PriceType; // prices of new lines; the lines keep the prices they were added with
  user_id: number | null; // the customer's account on the site
  buyer_name: string | null; // the organization it was bought for
};

export type OrderItem = {
  id: number;
  sku: number;
  name: string;
  price: number;
  supplier_price: number | null;
  qty: number;
  total: number;
  slug: string | null;
};

export type OrderEvent = { id: number; at: string; status: OrderStatus | null; note: string | null; author: string | null };

export type Order = OrderListItem & {
  organization_id: number | null;
  buyer: OrderBuyer | null; // requisites and contract as they were at checkout
  email: string | null;
  delivery: DeliveryMethod;
  city: string | null;
  address: string | null;
  comment: string | null;
  manager_note: string | null;
  items_total: number;
  delivery_price: number;
  discount: number;
  cost_total: number;
  margin: number;
  updated_at: string;
  items: OrderItem[];
  events: OrderEvent[];
};

export type OrderPage = Page<OrderListItem> & { counts: Partial<Record<OrderStatus, number>> };

export type CategoryRef = { id: number; name: string; slug: string };

export type AdminProduct = {
  sku: number;
  name: string;
  slug: string;
  vendor: string | null;
  part: string | null;
  category: CategoryRef | null;
  supplier_price: number | null;
  rrp: number | null;
  price: number | null; // retail cash: the shop price
  prices: Record<PriceType, number | null>;
  price_overrides: Partial<Record<PriceType, number>>; // only the types with a fixed price
  markup_percent: number | null; // of the retail cash price
  in_stock: boolean;
  qty: number;
  is_hidden: boolean;
  is_active: boolean;
  image: string | null;
};

// Decimal fields of the settings come as strings from the API ("20.0"); the forms send numbers back.
type Num = number | string;

/** Markup rules of one price type. */
export type PriceRules = {
  markup_percent: Num;
  category_markups: Record<string, Num>;
  min_markup: Num;
  round_to: number;
  use_rrp: boolean;
};

/** Markup rules of one supplier: one set per price type. */
export type SupplierPricing = Record<PriceType, PriceRules>;

export type AdminSupplier = { code: string; name: string; url: string; products: number; in_stock: number };

export type ShopSettings = {
  name: string;
  tagline: string;
  phone: string;
  email: string;
  address: string;
  working_hours: string;
  show_out_of_stock: boolean;
  gallery_min_side: number;
  gallery_dedupe: boolean;
};

export type AdminCategory = {
  id: number;
  parent_id: number | null;
  name: string;
  depth: number;
  product_count: number;
  is_hidden: boolean;
};

export type PricePreview = {
  markup_percent: number;
  markup_category: CategoryRef | null;
  by_markup: number;
  floor: number;
  rrp_used: boolean;
  before_rounding: number;
  price: number;
  margin: number;
  margin_percent: number;
  category_path: CategoryRef[];
  by_type: Record<PriceType, number>; // the same purchase priced by every type (the tried rules for one, the saved for the rest)
};

export type PriceImpact = {
  products: number;
  changed: number;
  up: number;
  down: number;
  avg_markup_before: number;
  avg_markup_after: number;
  examples: { sku: number; name: string; supplier_price: number; before: number | null; after: number }[];
  by_rrp: number; // of `products`: priced at the supplier's RRP rather than by the markup
  fixed: number; // left out: the price is fixed by hand
  above_retail: number | null; // wholesale types: products whose new price is above the retail price of the same payment
};

export type Job = {
  name: string;
  last_started_at: string | null;
  last_finished_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  info: Record<string, unknown>;
};

export type Dashboard = {
  products: { active: number; in_stock: number; hidden: number; fixed_price: number; in_stock_without_price: number };
  images: { by_status: Record<string, number>; by_variant: Record<string, number> };
  orders: {
    by_status: Partial<Record<OrderStatus, number>>;
    open: number;
    completed_30d: number;
    revenue_30d: number;
    margin_30d: number;
  };
  organizations: { pending: number }; // waiting for a manager's check
  recent_orders: OrderListItem[];
  jobs: Job[];
};

export type LabDetection = {
  template: string;
  rotated: boolean;
  x: number;
  y: number;
  score: number;
  strength: number;
  layers: number;
  opacity: number;
  outline: boolean;
  color: number | null;
};

export type LabResult = {
  width: number;
  height: number;
  format: string;
  chroma_subsampling: string;
  candidates: (LabDetection & { clipped: number; verdict: string | null })[];
  detection:
    | (LabDetection & {
        verdict: string;
        moved: [number, number] | null;
        box: [number, number, number, number];
        template_size: [number, number];
        left: number; // mark left after the removal, luma levels per unit of alpha
        left_before: number;
      })
    | null;
  plain?: { share: number; max: number };
  outline?: { plain: number; curve: number | null; used: boolean } | null;
  fit?: {
    pixels: number;
    grey: number;
    candidates: {
      layers: number;
      shared: boolean;
      opacity: number;
      colours: number[];
      left: number;
      plausible: boolean;
      chosen: boolean;
    }[];
  };
  timings_ms: { detect: number; remove?: number };
  images: Partial<Record<"original" | "cleaned" | "crop_before" | "crop_after" | "mask" | "diff", string>>;
};

export type LabSample = { image_id: number; sku: number; name: string; variant: string; thumb: string };

export type LabSampleGroup = { variant: string; total: number; items: LabSample[] };

export type FailedImage = { id: number; sku: number; source_url: string; error: string | null; attempts: number };

// --- catalog loading (backend/app/api/admin/loading.py) ---

export type ImageSettings = {
  paused: boolean;
  per_product: number;
  gallery: boolean;
  reprocess: boolean;
  download_rps: number;
};

export type SyncSettings = {
  stock_every_min: number;
  catalog_every_hours: number; // divides 24: the slots fall on the same hours every day
  catalog_hour: number; // local hour the catalog slots are counted from
};

/** The next run of a supplier sync, for the timers on the loading page. */
export type SyncTimer = {
  // due: its time has come; the worker takes it within half a minute, or after the sync it runs now (`busy`)
  state: "scheduled" | "due" | "running" | "retry" | "waiting";
  next_at: string | null;
  last_success_at: string | null;
  busy: string | null;
};

export type CheckStatus = "ok" | "warn" | "error" | "wait";
export type LoadingCheck = {
  key: string;
  title: string;
  status: CheckStatus;
  detail: string;
  hint: string | null;
  action: "retry" | null; // "retry": the failed supplier syncs can be run again at once
};

export type StageStatus = "done" | "running" | "waiting" | "paused" | "error" | "off";
export type LoadingStage = {
  key: string;
  title: string;
  detail: string;
  status: StageStatus;
  done: number | null;
  total: number | null;
  failed: number;
  estimated: boolean; // the total is partly estimated (photo lists not read yet)
  eta_seconds: number | null;
  progress?: number | null; // 0..1 of a running supplier sync, when it reports it
  phase?: string | null; // what that sync is doing now
  last_success_at?: string | null;
  error?: string | null;
  retry_at?: string | null; // a failed sync runs again at this time...
  retry_requested?: boolean; // ...or within half a minute, when asked
};

export type TrialStatus = "queued" | "running" | "done" | "interrupted";
export type LoadingTrial = {
  status: TrialStatus;
  requested: number;
  count?: number; // photos taken: fewer than requested when the catalog has fewer main photos
  requested_at: string | null;
  finished_at: string | null;
  done?: number;
  failed?: number;
  seconds?: number;
  ms_median?: number | null;
  download_rps?: number;
  image_workers?: number;
  capacity: number | null; // photos per second the CPU keeps up with
  ready?: number;
  failed_photos?: number;
  no_mark?: number;
  visible_left?: number;
};

export type Loading = {
  generated_at: string;
  version: number;
  settings: ImageSettings;
  sync: SyncSettings;
  schedule: { stock: SyncTimer; catalog: SyncTimer };
  limits: { download_rps_max: number; supplier_rps: number; web_concurrency: number };
  worker: {
    alive: boolean;
    heartbeat: string | null;
    started_at: string | null;
    activity: "starting" | "photos" | "idle" | "paused" | "trial" | null;
    outdated: boolean; // started before the backend code changed: restart it
    image_workers: number | null;
    cpus: number | null;
    photo_at: string | null; // the last photo it processed
    problem: { text: string; at: string } | null; // a trouble it ran into within the last hour
    pool_restarts: number; // times its photo processes were replaced (one died or hung)
  };
  overview: {
    products: number;
    in_stock: number;
    stock_photos: number; // in-stock products that have photos
    photos_done: number;
    photos_total: number;
    photos_estimated: boolean;
    eta_seconds: number | null;
    stalled_since: string | null; // the worker takes photos but none got processed since then
  };
  speed: { photos_per_second: number | null; source: "measured" | "expected"; last_10_min: number };
  checks: LoadingCheck[];
  stages: LoadingStage[];
  trial: LoadingTrial | null;
  quality: { checked: number; visible_left: number; no_mark: number; left_threshold: number };
  failed: { total: number; reasons: { reason: string; count: number }[] };
};

export type LoadingPhoto = {
  id: number;
  sku: number;
  name: string;
  status: string;
  wm_template: string | null;
  wm_left: number | null;
  error: string | null;
  thumb: string | null;
};

// --- organizations customers buy for (backend/app/api/admin/organizations.py) -----------------

export type OrganizationStatus = "pending" | "approved" | "rejected";
export type ContractTerms = "prepayment" | "deferred";

export type ContractRef = {
  id: number;
  number: string;
  signed_on: string; // YYYY-MM-DD
  terms: ContractTerms;
  deferral_days: number | null;
};

export type Contract = ContractRef & { is_active: boolean };
export type ContractInput = Omit<Contract, "id">;

export type OrganizationInput = { name: string; inn: string; kpp: string | null; ogrn: string | null; address: string | null };

/** The organization an order was placed for and its contract, as they were at checkout. */
export type OrderBuyer = OrganizationInput & { contract: ContractRef | null };

/** A user of the site working for an organization. */
export type Person = { id: number; email: string; name: string | null; phone: string | null };
/** owner: the head (requisites, people, every order); buyer: a purchaser (catalog, cart, own orders) */
export type MemberRole = "owner" | "buyer";
export type Member = Person & { role: MemberRole; added_at: string };

export type OrganizationListItem = {
  id: number;
  name: string;
  inn: string;
  kpp: string | null;
  status: OrganizationStatus;
  owner: Person | null; // its head
  members: number;
  contracts: number; // active ones
  created_at: string;
};

export type OrganizationPage = Page<OrganizationListItem> & { counts: Partial<Record<OrganizationStatus, number>> };

export type AdminOrganization = OrganizationInput & {
  id: number;
  status: OrganizationStatus;
  review_note: string | null; // why it was rejected; the customer sees it
  created_at: string;
  updated_at: string;
  reviewed_at: string | null;
  reviewed_by: string | null;
  members: Member[];
  contracts: Contract[];
  orders: OrderListItem[]; // the latest ones placed for it
  // other requests for the same company (ИНН, and КПП unless one has none): only one can be approved
  same_company: { id: number; name: string; kpp: string | null; status: OrganizationStatus; owner: Person | null }[];
};

// --- news and promotions (backend/app/api/admin/posts.py) ----------------------------------------

export type PostKind = "news" | "promo";

export type AdminPost = {
  id: number;
  kind: PostKind;
  title: string;
  body: string;
  link_url: string | null;
  is_published: boolean;
  published_at: string | null;
  pinned: boolean;
  ends_on: string | null; // a promotion's last day, YYYY-MM-DD
};

export type PostInput = Omit<AdminPost, "id" | "published_at">;
