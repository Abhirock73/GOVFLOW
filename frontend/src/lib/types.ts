export interface ProfileField<T = any> {
  value: T;
  confidence: number;
  source: 'voice' | 'id_photo' | 'user_edit';
}

export interface Address {
  line1?: string;
  village_or_city?: string;
  block?: string;
  district?: string;
  state?: string;
  pincode?: string;
}

export interface BankDetails {
  account_holder?: string;
  account_number?: string;
  ifsc?: string;
  bank_name?: string;
}

export interface CitizenProfile {
  name?: ProfileField<string>;
  dob?: ProfileField<string>;
  gender?: ProfileField<string>;
  id_type?: ProfileField<string>;
  id_number?: ProfileField<string>;
  address?: ProfileField<Address>;
  phone?: ProfileField<string>;
  email?: ProfileField<string>;
  income?: ProfileField<number | string>;
  category?: ProfileField<string>;
  bank_details?: ProfileField<BankDetails>;
  documents?: any[];
}

export interface ServiceRequest {
  raw_user_text: string;
  input_mode: 'link' | 'name';
  portal_url?: string;
  service_name?: string;
  resolution_confidence?: number;
}

export interface HumanRequest {
  request_id: string;
  kind: 'otp' | 'captcha' | 'login' | 'registration' | 'ambiguous_choice' | 'missing_data' | 'confirm_submit' | 'confirm_portal' | 'confirm_profile' | 'collect_missing';
  question: string;
  options?: string[];
  summary?: Record<string, string>;
}

export interface AgentStep {
  step_no: number;
  action: string;
  args?: Record<string, any>;
  rationale?: string;
  status: 'ok' | 'error' | 'blocked' | 'waiting_human';
  error?: string;
  url?: string;
  page_title?: string;
  summary?: string;
  thumbnail_b64?: string;
  cost_usd?: number;
}

export interface Receipt {
  job_id: string;
  reference_no: string;
  timestamp: string;
  portal: string;
  service_name?: string;
  submitted_fields_masked?: Record<string, string>;
  summary?: string;
}

export interface Job {
  job_id: string;
  status: string;
  service?: ServiceRequest;
  step_count: number;
  cost_usd: number;
  pending_request?: HumanRequest;
  receipt?: Receipt;
  error?: string;
}

