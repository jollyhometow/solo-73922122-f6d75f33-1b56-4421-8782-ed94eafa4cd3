-- 03_seed.sql
-- 去标识演示数据。授权分布：
--   课题 P1（肿瘤标志物研究）：S001-S006
--   课题 P2（代谢组学研究）  ：S004-S009
--   重叠授权：S004,S005,S006
--   仅 P1 可见：S001,S002,S003 ；仅 P2 可见：S007,S008,S009
--   S010 未授权给任何课题（用于验证默认拒绝）

INSERT INTO app.projects (project_id, project_name) VALUES
    ('P1', '肿瘤标志物研究'),
    ('P2', '代谢组学研究')
ON CONFLICT DO NOTHING;

INSERT INTO app.samples (sample_id, subject_pseudo_id, matrix, collected_on) VALUES
    ('S001', 'SUBJ-0001', 'PLASMA',      '2026-01-05'),
    ('S002', 'SUBJ-0002', 'SERUM',       '2026-01-06'),
    ('S003', 'SUBJ-0003', 'URINE',       '2026-01-08'),
    ('S004', 'SUBJ-0004', 'PLASMA',      '2026-01-10'),
    ('S005', 'SUBJ-0005', 'WHOLE_BLOOD', '2026-01-12'),
    ('S006', 'SUBJ-0006', 'SERUM',       '2026-01-13'),
    ('S007', 'SUBJ-0007', 'PLASMA',      '2026-01-15'),
    ('S008', 'SUBJ-0008', 'URINE',       '2026-01-16'),
    ('S009', 'SUBJ-0002', 'SERUM',       '2026-02-02'),
    ('S010', 'SUBJ-0009', 'PLASMA',      '2026-02-05')
ON CONFLICT DO NOTHING;

INSERT INTO app.project_sample_grant (project_id, sample_id) VALUES
    ('P1', 'S001'), ('P1', 'S002'), ('P1', 'S003'),
    ('P1', 'S004'), ('P1', 'S005'), ('P1', 'S006'),
    ('P2', 'S004'), ('P2', 'S005'), ('P2', 'S006'),
    ('P2', 'S007'), ('P2', 'S008'), ('P2', 'S009')
ON CONFLICT DO NOTHING;

-- 每样本 4 个检验项目，共 40 行明细
INSERT INTO app.lab_results (result_id, sample_id, analyte, value_num, unit, measured_on) VALUES
    (1001, 'S001', 'GLUCOSE',     5.20,  'mmol/L', '2026-01-06'),
    (1002, 'S001', 'HEMOGLOBIN',  142.0, 'g/L',    '2026-01-06'),
    (1003, 'S001', 'CREATININE',  71.0,  'umol/L', '2026-01-06'),
    (1004, 'S001', 'ALT',         22.0,  'U/L',    '2026-01-06'),
    (1005, 'S002', 'GLUCOSE',     6.10,  'mmol/L', '2026-01-07'),
    (1006, 'S002', 'HEMOGLOBIN',  135.0, 'g/L',    '2026-01-07'),
    (1007, 'S002', 'CREATININE',  88.0,  'umol/L', '2026-01-07'),
    (1008, 'S002', 'ALT',         41.0,  'U/L',    '2026-01-07'),
    (1009, 'S003', 'GLUCOSE',     4.80,  'mmol/L', '2026-01-09'),
    (1010, 'S003', 'HEMOGLOBIN',  151.0, 'g/L',    '2026-01-09'),
    (1011, 'S003', 'CREATININE',  64.0,  'umol/L', '2026-01-09'),
    (1012, 'S003', 'ALT',         18.0,  'U/L',    '2026-01-09'),
    (1013, 'S004', 'GLUCOSE',     7.40,  'mmol/L', '2026-01-11'),
    (1014, 'S004', 'HEMOGLOBIN',  128.0, 'g/L',    '2026-01-11'),
    (1015, 'S004', 'CREATININE',  95.0,  'umol/L', '2026-01-11'),
    (1016, 'S004', 'ALT',         55.0,  'U/L',    '2026-01-11'),
    (1017, 'S005', 'GLUCOSE',     5.60,  'mmol/L', '2026-01-13'),
    (1018, 'S005', 'HEMOGLOBIN',  119.0, 'g/L',    '2026-01-13'),
    (1019, 'S005', 'CREATININE',  77.0,  'umol/L', '2026-01-13'),
    (1020, 'S005', 'ALT',         30.0,  'U/L',    '2026-01-13'),
    (1021, 'S006', 'GLUCOSE',     4.95,  'mmol/L', '2026-01-14'),
    (1022, 'S006', 'HEMOGLOBIN',  138.0, 'g/L',    '2026-01-14'),
    (1023, 'S006', 'CREATININE',  69.0,  'umol/L', '2026-01-14'),
    (1024, 'S006', 'ALT',         26.0,  'U/L',    '2026-01-14'),
    (1025, 'S007', 'GLUCOSE',     8.20,  'mmol/L', '2026-01-16'),
    (1026, 'S007', 'HEMOGLOBIN',  124.0, 'g/L',    '2026-01-16'),
    (1027, 'S007', 'CREATININE',  102.0, 'umol/L', '2026-01-16'),
    (1028, 'S007', 'ALT',         63.0,  'U/L',    '2026-01-16'),
    (1029, 'S008', 'GLUCOSE',     5.05,  'mmol/L', '2026-01-17'),
    (1030, 'S008', 'HEMOGLOBIN',  146.0, 'g/L',    '2026-01-17'),
    (1031, 'S008', 'CREATININE',  58.0,  'umol/L', '2026-01-17'),
    (1032, 'S008', 'ALT',         15.0,  'U/L',    '2026-01-17'),
    (1033, 'S009', 'GLUCOSE',     5.90,  'mmol/L', '2026-02-03'),
    (1034, 'S009', 'HEMOGLOBIN',  133.0, 'g/L',    '2026-02-03'),
    (1035, 'S009', 'CREATININE',  84.0,  'umol/L', '2026-02-03'),
    (1036, 'S009', 'ALT',         37.0,  'U/L',    '2026-02-03'),
    (1037, 'S010', 'GLUCOSE',     9.10,  'mmol/L', '2026-02-06'),
    (1038, 'S010', 'HEMOGLOBIN',  110.0, 'g/L',    '2026-02-06'),
    (1039, 'S010', 'CREATININE',  120.0, 'umol/L', '2026-02-06'),
    (1040, 'S010', 'ALT',         72.0,  'U/L',    '2026-02-06')
ON CONFLICT DO NOTHING;
