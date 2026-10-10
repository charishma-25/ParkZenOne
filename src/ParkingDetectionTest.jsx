import { useState } from "react";

function ParkingDetectionTest() {
  const [image, setImage] = useState(null);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async () => {
    if (!image) {
      alert("Please select a parking image");
      return;
    }

    setLoading(true);
    setResult(null);

    const formData = new FormData();
    formData.append("image", image);

    try {
      const response = await fetch(
        "http://127.0.0.1:5000/predict",
        {
          method: "POST",
          body: formData,
        }
      );

      const data = await response.json();

      console.log("ParkZen AI Result:", data);

      setResult(data);

    } catch (error) {
      console.error(error);
      alert("Could not connect to ParkZen AI backend.");
    }

    setLoading(false);
  };

  return (
    <div style={{ padding: "40px" }}>

      <h1>ParkZen AI Detection Test</h1>

      <p>
        Upload a parking image and let YOLO11 detect
        empty and occupied parking spaces.
      </p>

      <input
        type="file"
        accept="image/*"
        onChange={(e) => setImage(e.target.files[0])}
      />

      <br />
      <br />

      <button onClick={handleSubmit}>
        {loading ? "Analyzing..." : "Analyze Parking"}
      </button>

      {result && (
        <div style={{ marginTop: "30px" }}>

          <h2>Parking Status</h2>

          <p>
            Total Slots: {result.total_slots}
          </p>

          <p>
            Empty: {result.empty}
          </p>

          <p>
            Occupied: {result.occupied}
          </p>

          <p>
            Occupancy: {result.occupancy}%
          </p>
        <p>
  Recommended Slot: {result.recommended_slot || "No slot available"}
</p>
        </div>
      )}

    </div>
  );
}

export default ParkingDetectionTest;