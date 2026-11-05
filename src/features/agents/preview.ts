import type { AgentArtifact, ArtifactPreviewKind } from './types';

// The server labels a picture 'image' regardless of the extension it came in
// with, so both spellings have to land on the same renderer.
const IMAGE_FORMATS = new Set(['image', 'png', 'jpg', 'jpeg', 'gif', 'webp']);

// The server reads what it can out of a document and says so in `previewKind`;
// everything else is decided from the format. One place decides, so adding a
// format never means hunting down the four components that would have to agree.
export function previewKind(artifact: AgentArtifact): ArtifactPreviewKind {
  const declared = artifact.previewKind;
  if (declared === 'sheets' || declared === 'slides' || declared === 'document') return declared;
  const format = String(artifact.format || '').toLowerCase();
  if (format === 'pdf') return 'pdf';
  if (format === 'html' || format === 'htm') return 'html';
  if (IMAGE_FORMATS.has(format)) return 'image';
  return 'none';
}

export function canPreview(artifact: AgentArtifact): boolean {
  return Boolean(artifact.draft) || previewKind(artifact) !== 'none';
}

export function formatLabel(artifact: AgentArtifact): string {
  const format = String(artifact.format || '').toLowerCase();
  if (format === 'xlsx') return 'Excel';
  if (format === 'pptx') return 'PPT';
  if (format === 'docx') return 'Word';
  if (IMAGE_FORMATS.has(format)) return 'Image';
  return format.toUpperCase();
}
