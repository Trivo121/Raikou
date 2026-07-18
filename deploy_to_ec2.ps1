$PemKey = "D:\Study\Projects\Raikou\hackeval-key_AWS_key_value_pair.pem"
$Ec2User = "ubuntu"
$Ec2Host = "65.0.32.233"
$LocalBackend = "D:\Study\Projects\Raikou\backend"
$RemoteBackend = "/home/ubuntu/backend"

Write-Host "Deploying backend to EC2..."
scp -i $PemKey -o StrictHostKeyChecking=no -r $LocalBackend\app $Ec2User@${Ec2Host}:$RemoteBackend
scp -i $PemKey -o StrictHostKeyChecking=no -r $LocalBackend\scripts $Ec2User@${Ec2Host}:$RemoteBackend
scp -i $PemKey -o StrictHostKeyChecking=no $LocalBackend\requirements.txt $Ec2User@${Ec2Host}:$RemoteBackend
scp -i $PemKey -o StrictHostKeyChecking=no $LocalBackend\main.py $Ec2User@${Ec2Host}:$RemoteBackend

Write-Host "Deployment complete."
