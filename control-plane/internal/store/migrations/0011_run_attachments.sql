-- 会话轮附件持久化（补 M8 已知限制）：POST /runs 请求体里的 attachments 数组
-- （AttachmentRef：resourceKey/fileName/mimeType/size/previewUrl/downloadUrl）原样 JSON 落进
-- runs 行；GET /sessions/{id}/runs 原样返还。此前该元数据只活在实时流里——刷新/重进会话后
-- 用户气泡下的附件 chips 与工作区「上传内容」段全部消失。文件本体一直在 MinIO
-- uploads/<owner>/<session>/… 永存，本列只补「哪一轮带了哪些附件」的账。NULL=该轮无附件。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS attachments JSONB;
