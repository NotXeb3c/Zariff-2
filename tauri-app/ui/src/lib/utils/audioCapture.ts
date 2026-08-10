/**
 * Acquire a microphone stream only while the request that started it is still
 * current. Browser permission prompts can resolve after a component has been
 * stopped or destroyed; those late streams must be closed immediately.
 */
export async function acquireCurrentMicrophone(
  requestStream: () => Promise<MediaStream>,
  isCurrent: () => boolean,
): Promise<MediaStream | null> {
  const stream = await requestStream();
  if (isCurrent()) return stream;

  stream.getTracks().forEach((track) => track.stop());
  return null;
}
