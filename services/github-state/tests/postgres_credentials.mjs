export function testCredentials(){
 try {
  const application=new URL(process.env.TEST_POSTGRES_URL),migration=new URL(process.env.TEST_POSTGRES_MIGRATION_URL);
  if(!['postgres:','postgresql:'].includes(application.protocol) || !['postgres:','postgresql:'].includes(migration.protocol) || !decodeURIComponent(application.username).includes('_test_') || !decodeURIComponent(migration.username).includes('_test_'))throw Error();
  return {application,migration};
 }catch{throw Error('Isolated PostgreSQL test application and migration credentials are required');}
}
