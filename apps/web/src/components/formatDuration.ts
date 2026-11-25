export function formatDuration(seconds: number | null | undefined) {
  if (seconds == null) return "미확인";
  const tenths = Math.round(seconds * 10);
  const hours = Math.floor(tenths / 36000);
  const minutes = Math.floor((tenths % 36000) / 600);
  const remainingSeconds = (tenths % 600) / 10;
  if (hours) return `${hours}시간 ${minutes}분 ${remainingSeconds}초`;
  if (minutes) return `${minutes}분 ${remainingSeconds}초`;
  return `${remainingSeconds}초`;
}
