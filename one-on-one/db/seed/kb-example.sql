-- Example KB facts (fictional). Replace with your real product/service info.
--
-- Convention:
--   - one slug per product/service (e.g. 'service-consultation', 'service-workshop')
--   - special slug '_global' for info not tied to a single service
--
-- Run after `make db-migrate`:
--   psql -U iris -d iris -f db/seed/kb-example.sql

INSERT INTO kb_facts (kb_slug, key, value, source) VALUES
  ('service-consultation', 'name',          '1:1 Consultation',                                           'manual'),
  ('service-consultation', 'duration',      '60 minutes',                                                 'manual'),
  ('service-consultation', 'price',         '$100 USD',                                                   'manual'),
  ('service-consultation', 'modality',      'video call or in person',                                    'manual'),
  ('service-consultation', 'landing_url',   'https://example.com/consultation',                           'landing'),
  ('service-consultation', 'audience',      'individuals seeking guidance',                               'manual'),

  ('service-workshop',     'name',          'Group Workshop',                                             'manual'),
  ('service-workshop',     'duration',      '3 hours',                                                    'manual'),
  ('service-workshop',     'price',         '$250 USD per participant',                                   'manual'),
  ('service-workshop',     'modality',      'in person',                                                  'manual'),
  ('service-workshop',     'landing_url',   'https://example.com/workshop',                               'landing'),

  ('_global',              'admin_contact', '+1 555 000 1111 (Jane, Admin Assistant)',                    'manual'),
  ('_global',              'site',          'https://example.com',                                        'landing'),
  ('_global',              'emergency_line','911 (or your local emergency number)',                       'manual')
ON CONFLICT (kb_slug, key) DO UPDATE
  SET value = EXCLUDED.value,
      source = EXCLUDED.source;
