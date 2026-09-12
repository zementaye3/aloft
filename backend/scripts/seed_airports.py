"""
Seed script to populate the airports collection with major world airports.

This script loads the static airport data from airport_repository.py
and inserts it into MongoDB for faster location-based queries.

Usage:
    python scripts/seed_airports.py
"""

import asyncio
import sys
from pathlib import Path

# Add the backend directory to the path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from app.core.config import get_settings  # noqa: E402
from app.core.db import close_mongo_connection, connect_to_mongo, get_db  # noqa: E402
from app.models.airport import Airport  # noqa: E402
from app.services.airport_repository import _STATIC_AIRPORTS  # noqa: E402

# Enhanced airport data with city and country information
_AIRPORT_NAMES = {
    # Africa
    "ADD": ("Addis Ababa Bole International Airport", "Addis Ababa", "Ethiopia"),
    "NBO": ("Jomo Kenyatta International Airport", "Nairobi", "Kenya"),
    "JNB": ("OR Tambo International Airport", "Johannesburg", "South Africa"),
    "CPT": ("Cape Town International Airport", "Cape Town", "South Africa"),
    "LOS": ("Murtala Muhammed International Airport", "Lagos", "Nigeria"),
    "CAI": ("Cairo International Airport", "Cairo", "Egypt"),
    "CMN": ("Mohammed V International Airport", "Casablanca", "Morocco"),
    "ABJ": ("Félix Houphouët-Boigny International Airport", "Abidjan", "Ivory Coast"),
    "ACC": ("Kotoka International Airport", "Accra", "Ghana"),
    "DAR": ("Julius Nyerere International Airport", "Dar es Salaam", "Tanzania"),
    "KGL": ("Kigali International Airport", "Kigali", "Rwanda"),
    "EBB": ("Entebbe International Airport", "Entebbe", "Uganda"),
    # Middle East
    "DXB": ("Dubai International Airport", "Dubai", "United Arab Emirates"),
    "AUH": ("Abu Dhabi International Airport", "Abu Dhabi", "United Arab Emirates"),
    "DOH": ("Hamad International Airport", "Doha", "Qatar"),
    "RUH": ("King Khalid International Airport", "Riyadh", "Saudi Arabia"),
    "JED": ("King Abdulaziz International Airport", "Jeddah", "Saudi Arabia"),
    "AMM": ("Queen Alia International Airport", "Amman", "Jordan"),
    "BEY": ("Beirut-Rafic Hariri International Airport", "Beirut", "Lebanon"),
    "KWI": ("Kuwait International Airport", "Kuwait City", "Kuwait"),
    "BAH": ("Bahrain International Airport", "Bahrain", "Bahrain"),
    "MCT": ("Muscat International Airport", "Muscat", "Oman"),
    # Europe
    "LHR": ("London Heathrow Airport", "London", "United Kingdom"),
    "CDG": ("Charles de Gaulle Airport", "Paris", "France"),
    "FRA": ("Frankfurt Airport", "Frankfurt", "Germany"),
    "AMS": ("Amsterdam Schiphol Airport", "Amsterdam", "Netherlands"),
    "MAD": ("Adolfo Suárez Madrid-Barajas Airport", "Madrid", "Spain"),
    "BCN": ("Barcelona-El Prat Airport", "Barcelona", "Spain"),
    "FCO": ("Leonardo da Vinci–Fiumicino Airport", "Rome", "Italy"),
    "MXP": ("Milan Malpensa Airport", "Milan", "Italy"),
    "MUC": ("Munich Airport", "Munich", "Germany"),
    "ZRH": ("Zürich Airport", "Zürich", "Switzerland"),
    "VIE": ("Vienna International Airport", "Vienna", "Austria"),
    "BRU": ("Brussels Airport", "Brussels", "Belgium"),
    "ARN": ("Stockholm Arlanda Airport", "Stockholm", "Sweden"),
    "CPH": ("Copenhagen Airport", "Copenhagen", "Denmark"),
    "OSL": ("Oslo Gardermoen Airport", "Oslo", "Norway"),
    "HEL": ("Helsinki-Vantaa Airport", "Helsinki", "Finland"),
    "IST": ("Istanbul Airport", "Istanbul", "Turkey"),
    "ATH": ("Athens International Airport", "Athens", "Greece"),
    "LIS": ("Lisbon Humberto Delgado Airport", "Lisbon", "Portugal"),
    # Asia
    "SIN": ("Singapore Changi Airport", "Singapore", "Singapore"),
    "BKK": ("Suvarnabhumi Airport", "Bangkok", "Thailand"),
    "KUL": ("Kuala Lumpur International Airport", "Kuala Lumpur", "Malaysia"),
    "CGK": ("Soekarno-Hatta International Airport", "Jakarta", "Indonesia"),
    "MNL": ("Ninoy Aquino International Airport", "Manila", "Philippines"),
    "ICN": ("Incheon International Airport", "Seoul", "South Korea"),
    "NRT": ("Narita International Airport", "Tokyo", "Japan"),
    "HND": ("Haneda Airport", "Tokyo", "Japan"),
    "PEK": ("Beijing Capital International Airport", "Beijing", "China"),
    "PVG": ("Shanghai Pudong International Airport", "Shanghai", "China"),
    "DEL": ("Indira Gandhi International Airport", "Delhi", "India"),
    "BOM": ("Chhatrapati Shivaji Maharaj International Airport", "Mumbai", "India"),
    "BLR": ("Kempegowda International Airport", "Bangalore", "India"),
    "MAA": ("Chennai International Airport", "Chennai", "India"),
    "CCU": ("Netaji Subhas Chandra Bose International Airport", "Kolkata", "India"),
    "KHI": ("Jinnah International Airport", "Karachi", "Pakistan"),
    "LHE": ("Allama Iqbal International Airport", "Lahore", "Pakistan"),
    "ISB": ("Islamabad International Airport", "Islamabad", "Pakistan"),
    "CMB": ("Bandaranaike International Airport", "Colombo", "Sri Lanka"),
    "DAC": ("Hazrat Shahjalal International Airport", "Dhaka", "Bangladesh"),
    "KTM": ("Tribhuvan International Airport", "Kathmandu", "Nepal"),
    "RGN": ("Yangon International Airport", "Yangon", "Myanmar"),
    # North America
    "JFK": ("John F. Kennedy International Airport", "New York", "United States"),
    "LAX": ("Los Angeles International Airport", "Los Angeles", "United States"),
    "ORD": ("O'Hare International Airport", "Chicago", "United States"),
    "DFW": ("Dallas/Fort Worth International Airport", "Dallas", "United States"),
    "MIA": ("Miami International Airport", "Miami", "United States"),
    "SFO": ("San Francisco International Airport", "San Francisco", "United States"),
    "ATL": ("Hartsfield-Jackson Atlanta International Airport", "Atlanta", "United States"),
    "YYZ": ("Toronto Pearson International Airport", "Toronto", "Canada"),
    "YVR": ("Vancouver International Airport", "Vancouver", "Canada"),
    "MEX": ("Mexico City International Airport", "Mexico City", "Mexico"),
    # South America
    "GRU": ("São Paulo/Guarulhos International Airport", "São Paulo", "Brazil"),
    "EZE": ("Ministro Pistarini International Airport", "Buenos Aires", "Argentina"),
    "SCL": ("Arturo Merino Benítez International Airport", "Santiago", "Chile"),
    "BOG": ("El Dorado International Airport", "Bogotá", "Colombia"),
    "LIM": ("Jorge Chávez International Airport", "Lima", "Peru"),
    # Oceania
    "SYD": ("Sydney Kingsford Smith Airport", "Sydney", "Australia"),
    "MEL": ("Melbourne Airport", "Melbourne", "Australia"),
    "BNE": ("Brisbane Airport", "Brisbane", "Australia"),
    "AKL": ("Auckland Airport", "Auckland", "New Zealand"),
}


async def seed_airports():
    """Seed the airports collection with static airport data."""
    settings = get_settings()

    print(f"Connecting to MongoDB: {settings.mongodb_uri}")
    await connect_to_mongo()

    db = get_db()

    # Clear existing airports (optional - comment out if you want to keep existing data)
    print("Clearing existing airports collection...")
    await db.airports.delete_many({})

    # Prepare airport documents
    airports_to_insert = []
    for iata_code, (lat, lng) in _STATIC_AIRPORTS.items():
        # Get enhanced name data if available
        name, city, country = _AIRPORT_NAMES.get(iata_code, (iata_code, None, None))

        airport = Airport(
            iata_code=iata_code,
            name=name,
            lat=lat,
            lng=lng,
            city=city,
            country=country,
        )
        airports_to_insert.append(airport.to_mongo_dict())

    # Insert airports
    print(f"Inserting {len(airports_to_insert)} airports...")
    if airports_to_insert:
        result = await db.airports.insert_many(airports_to_insert)
        print(f"Successfully inserted {len(result.inserted_ids)} airports")

    # Create geospatial index for location queries
    print("Creating geospatial index...")
    await db.airports.create_index([("location", "2dsphere")])

    # Create index for IATA code lookups
    print("Creating IATA code index...")
    await db.airports.create_index([("iata_code", 1)], unique=True)

    print("✅ Airport seeding completed successfully!")

    await close_mongo_connection()


if __name__ == "__main__":
    asyncio.run(seed_airports())
