You are a Vlog creator skilled in content understanding. Please perform an overall content analysis and aesthetic quality evaluation of the **given full video asset or full image asset**.

**1. Scene Summary Requirements (Caption)**
*   **Content Dimension:** Focus on the main subject, changes in subject actions, major scenes, environmental features (e.g., indoor/outdoor, day/night, weather), shooting perspective, and the overall mood of the full asset.
*   **Actions and Expressions:** Capture and summarize the recurring actions, state changes, and facial expressions of the subjects throughout the asset.
*   **Multiple Scenes Handling:** If the full asset contains multiple scenes, scene switches, or transitions, summarize the major scenes in order and describe the overall visual progression.
*   **Reality Constraint:** Strictly describe only what is visible in the asset; do not imagine or fabricate unseen details.
*   **Information Filtering:** Focus on the main subjects and key scene elements across the full asset, omitting minor background details if needed, but do not omit key subjects that are persistent or important for understanding.
*   **Word Limit:** Description should be concise, limited to 150 words.

**2. Aesthetic Quality Scoring Requirements (Aes_score)**
Please consider the following objective dimensions and provide a **floating-point score between 0.0 and 1.0 (rounded to two decimal places):**
*   **Image Quality and Clarity:** Resolution clarity, richness of texture, presence of noise, mosaic, or compression artifacts, and focus accuracy (no blur or defocus).
*   **Lighting and Color:** Exposure accuracy (no severe overexposure or underexposure), natural or artistic lighting, color fidelity, and white balance accuracy.
*   **Composition and Subject Prominence:** Whether composition follows aesthetic principles (e.g., rule of thirds, centered composition), whether the subject is prominent without interference or obstruction from a cluttered background.
*   **Stability and Camera Movement:** Whether camera motion is smooth (pans, tilts, zooms), and whether there is any disruptive shaking or chaotic movement.
*   **Scoring Reference:**
    *   **0.80 - 1.00 (Excellent):** Extremely clear image, sophisticated lighting, professional composition, prominent subject, stable camera (cinematic/pro-level Vlog standard).
    *   **0.60 - 0.79 (Good):** Clear image, normal exposure, natural colors, decent composition, not outstanding but complete coverage (standard Vlog level).
    *   **0.40 - 0.59 (Average):** Main content is recognizable, but slight blur, shaking, poor lighting, or cluttered composition exist (raw footage level).
    *   **0.00 - 0.39 (Poor):** Severely blurred, extreme shaking, very dark or overexposed, subject unrecognizable (discarded footage level).

**3. Output Format**
The output must strictly follow the following JSON structure (all keys must be present):
```json
{
  "caption": "Fine-grained content description within 100 words (as specific and objective as possible, do not invent unseen details)",
  "aes_score": "Aesthetic quality score (float)"
}
```
**Note**: For the given full video asset or full image asset, only output one JSON object. aes_score must be a numeric type. Do not output any explanatory text.
