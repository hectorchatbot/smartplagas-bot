@echo off
echo Enviando 'hola'...
curl -X POST "https://web-production-62037.up.railway.app/webhook" -H "Content-Type: application/x-www-form-urlencoded" -d "From=%2B56912345678&Body=hola"
echo.

echo Enviando nombre...
curl -X POST "https://web-production-62037.up.railway.app/webhook" -H "Content-Type: application/x-www-form-urlencoded" -d "From=%2B56912345678&Body=Juan"
echo.

echo Elegir opción 1 - Control de plagas...
curl -X POST "https://web-production-62037.up.railway.app/webhook" -H "Content-Type: application/x-www-form-urlencoded" -d "From=%2B56912345678&Body=1"
echo.

echo Elegir opción 1 - Desratización...
curl -X POST "https://web-production-62037.up.railway.app/webhook" -H "Content-Type: application/x-www-form-urlencoded" -d "From=%2B56912345678&Body=1"
echo.

echo Elegir opción 3 - Ambas zonas...
curl -X POST "https://web-production-62037.up.railway.app/webhook" -H "Content-Type: application/x-www-form-urlencoded" -d "From=%2B56912345678&Body=3"
echo.

echo Elegir metros cuadrados - menos de 100 m2...
curl -X POST "https://web-production-62037.up.railway.app/webhook" -H "Content-Type: application/x-www-form-urlencoded" -d "From=%2B56912345678&Body=1"
echo.

echo Ingresar dirección...
curl -X POST "https://web-production-62037.up.railway.app/webhook" -H "Content-Type: application/x-www-form-urlencoded" -d "From=%2B56912345678&Body=Av+Siempre+Viva+123"
echo.

echo Ingresar comuna...
curl -X POST "https://web-production-62037.up.railway.app/webhook" -H "Content-Type: application/x-www-form-urlencoded" -d "From=%2B56912345678&Body=Villarrica"
echo.

echo Ingresar email...
curl -X POST "https://web-production-62037.up.railway.app/webhook" -H "Content-Type: application/x-www-form-urlencoded" -d "From=%2B56912345678&Body=juan@email.com"
echo.

echo Ingresar teléfono...
curl -X POST "https://web-production-62037.up.railway.app/webhook" -H "Content-Type: application/x-www-form-urlencoded" -d "From=%2B56912345678&Body=912345678"
echo.

pause
