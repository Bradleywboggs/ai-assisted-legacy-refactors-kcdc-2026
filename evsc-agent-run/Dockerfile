FROM php:8.2-cli

RUN docker-php-ext-install pdo_mysql

WORKDIR /app
COPY . .

CMD ["php", "bin/worker.php"]
